"""Spend caps for a publicly reachable LLM endpoint.

THE THREAT IS NOT ABUSE, IT IS A LINK THAT WORKS
------------------------------------------------
This app is meant to be reachable from a banner on a public site, which means
anyone can spend the account's Gemini tokens by typing in a box. No login stands
in the way, by design -- a showcase nobody can try is not a showcase. So the
budget has to be structural rather than a matter of trusting visitors.

Three caps, each closing a hole the others leave open:

  per-session questions   stops one bored visitor looping all afternoon
  daily global tokens     stops a hundred visitors, or one scripted client,
                          from adding up to a surprise
  agent step limit        stops a single question from spiralling -- an agent
                          that keeps retrying a failing query is the expensive
                          failure mode, and it is invisible from a question count

FAILS CLOSED. If the ledger cannot be read or written, the app refuses to spend
rather than assuming there is budget left. A corrupted state file must not be a
blank cheque.

WHY A FILE AND NOT st.session_state
-----------------------------------
`st.session_state` is per browser session, so it cannot see the total. A module
global cannot either: it dies on restart, and Streamlit restarts on every code
change. The daily counter therefore lives on disk, guarded by `flock`, so it
survives restarts and stays correct with several Streamlit workers on one box.

Postgres would also work, but the ledger must be writable and the only database
role available here is deliberately read-only -- adding a writable role to fix a
budget counter would put a hole in the thing that makes the whole app safe.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path

# Where the ledger lives. /var/lib on the box, a repo-local file in development.
STATE_PATH = Path(
    os.environ.get("SQLX_STATE_PATH", Path(__file__).parent / ".budget.json")
)

# Caps. All three are env-overridable so the box can run tighter limits than a
# laptop without a code change.
MAX_QUESTIONS_PER_SESSION = int(os.environ.get("SQLX_MAX_SESSION_QUESTIONS", "12"))
MAX_TOKENS_PER_DAY = int(os.environ.get("SQLX_MAX_DAILY_TOKENS", "2000000"))
# 28, raised from 20 on 2026-09-08 with the move to Gemini 3.8 Flash, which
# needs more agent steps than 3.7 did for the same work. Measured on the same
# Denver question, against the live MCP server: 3.7 took 16/17/17/20 steps and
# fitted under 20; 3.8 took 20/22/22/30 and did not, failing outright on two
# further runs. reasoning_effort=low (see agent.py) pulls it back to 17/17/21 --
# better, but still over 20 on one run in four, so the effort setting alone does
# not fix this. 28 clears the worst measured run at low effort with headroom and
# still catches a genuine spiral. Verified after deploying both changes, five
# runs through the same build_agent/ask path the app uses: 18/24/19/22/20 steps,
# 5 of 5 answered, 52-65k tokens. The history below is why the boundary matters.
#
# 20, raised from 14 on 2026-08-21. 14 was cutting off legitimate questions
# rather than only runaways, and it was doing it right at the boundary. The
# example question "the daily average temperature at Denver for the last 30 days"
# needs four queries -- resolve the station name, find the latest date, then the
# aggregate -- and measured at EXACTLY 14 agent steps, which against a limit of
# 14 fails or succeeds depending on where the final answer message lands. So it
# failed intermittently, on a button on the landing page.
#
# A question that dies on the step limit still charges the visitor and returns
# nothing, which is the worst outcome available; 20 leaves that question six
# steps of headroom while still catching a genuine retry spiral.
#
# This raises the worst-case steps per question, not the worst-case spend:
# MAX_TOKENS_PER_QUESTION below still bounds any single question, and the daily
# total is bounded by MAX_TOKENS_PER_DAY regardless.
MAX_AGENT_STEPS = int(os.environ.get("SQLX_MAX_AGENT_STEPS", "28"))

# A single question that somehow burns more than this is a runaway; charge it and
# move on rather than letting an unbounded number land on the daily total.
MAX_TOKENS_PER_QUESTION = int(os.environ.get("SQLX_MAX_QUESTION_TOKENS", "120000"))


class BudgetExceeded(RuntimeError):
    """Raised when a question must not be answered. The message is shown to the user."""


def _today() -> str:
    # UTC, not local: the box runs UTC and the reset must not move twice a year.
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _read_locked(handle) -> dict:
    """Read the ledger from an already-locked handle, tolerating a fresh file."""
    handle.seek(0)
    raw = handle.read()
    if not raw.strip():
        return {"date": _today(), "tokens": 0, "questions": 0}
    return json.loads(raw)


def _write_locked(handle, state: dict) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(state, handle)
    handle.flush()
    os.fsync(handle.fileno())


def _with_ledger(mutate):
    """Run `mutate(state) -> state` under an exclusive lock on the ledger.

    Opened "a+" so the file is created if absent without truncating it if
    present. flock is held across read-modify-write, which is what makes the
    counter correct when two workers answer questions at the same instant --
    read-then-write without the lock loses one of the two increments.
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_locked(handle)
            # Roll over on the first access of a new day.
            if state.get("date") != _today():
                state = {"date": _today(), "tokens": 0, "questions": 0}
            state = mutate(state)
            _write_locked(handle, state)
            return state
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def status() -> dict:
    """Current spend, for display. Never raises -- a broken ledger reads as spent.

    Reporting "fully spent" on an unreadable ledger is deliberate: it matches what
    `check_can_spend` will do, so the UI and the gate agree rather than the banner
    promising budget that the next question is refused for.
    """
    try:
        state = _with_ledger(lambda s: s)
        used = int(state.get("tokens", 0))
        return {
            "date": state.get("date"),
            "tokens_used": used,
            "tokens_limit": MAX_TOKENS_PER_DAY,
            "remaining": max(0, MAX_TOKENS_PER_DAY - used),
            "questions_today": int(state.get("questions", 0)),
            "healthy": True,
        }
    except Exception as exc:  # noqa: BLE001 - must not take the page down
        return {
            "date": _today(),
            "tokens_used": MAX_TOKENS_PER_DAY,
            "tokens_limit": MAX_TOKENS_PER_DAY,
            "remaining": 0,
            "questions_today": 0,
            "healthy": False,
            "error": str(exc),
        }


def check_can_spend(session_questions: int) -> None:
    """Raise BudgetExceeded unless another question may be answered now.

    Called BEFORE the model runs. The daily check uses tokens already recorded,
    so the cap can be overshot by at most one question's worth -- bounded by
    MAX_TOKENS_PER_QUESTION. Reserving tokens up front instead would need a
    refund path for every failure, and a crashed request would leak reservations
    until midnight.
    """
    if session_questions >= MAX_QUESTIONS_PER_SESSION:
        raise BudgetExceeded(
            f"This session has reached its limit of {MAX_QUESTIONS_PER_SESSION} "
            "questions. Reload the page to start a new one."
        )

    try:
        state = _with_ledger(lambda s: s)
    except Exception as exc:  # noqa: BLE001
        raise BudgetExceeded(
            "The usage ledger could not be read, so no request will be sent. "
            f"({exc})"
        ) from exc

    used = int(state.get("tokens", 0))
    if used >= MAX_TOKENS_PER_DAY:
        raise BudgetExceeded(
            "This demo has reached its daily usage budget and will reset at "
            "00:00 UTC. The database and the MCP server are unaffected -- only "
            "the language model is paused."
        )


def record(tokens: int) -> dict:
    """Charge a completed question against today's budget.

    Called even when the question failed: a failed agent run still burns tokens,
    and not charging failures is how a broken query loop escapes the budget.
    """
    charged = max(0, min(int(tokens or 0), MAX_TOKENS_PER_QUESTION))

    def mutate(state: dict) -> dict:
        state["tokens"] = int(state.get("tokens", 0)) + charged
        state["questions"] = int(state.get("questions", 0)) + 1
        return state

    try:
        return _with_ledger(mutate)
    except Exception:  # noqa: BLE001
        # Losing an increment is bad but must not lose the user's answer. The
        # next check_can_spend will fail closed if the file is still unreadable.
        return status()
