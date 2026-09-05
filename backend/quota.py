"""
Local daily-quota tracker for Gemini's free-tier request cap.

Gemini's free tier currently allows only 20 requests/day for the resolved
flash model (confirmed directly against a live key on 2026-09-05 — this
number has tightened before and may again; treat it as a safety margin,
not a guarantee). Waiting for Gemini itself to reject a request wastes a
slot and shows the user a raw API error. Instead, this module counts
successful vision calls locally per UTC day and lets main.py refuse new
checks once the budget is likely spent — before spending an API call to
find out.

This is a soft local safety margin, not a substitute for Gemini's own
quota enforcement: a restart clears in-memory state (falls back to
disk-persisted count), and concurrent processes would each track
separately. Fine for a single free-tier instance; would need a shared
store (Redis, a database) if this ever runs as multiple instances.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).parent.parent / "data" / "quota_state.json"

# Deliberately set below Gemini's actual 20/day cap so a few requests stay
# available for retries on transient errors and for /api/health checks,
# rather than the app's own counter being the exact thing that hits zero
# right as Gemini's real limit does.
DAILY_LIMIT = 16

_lock = threading.Lock()


@dataclass
class QuotaStatus:
    used: int
    limit: int
    remaining: int
    exhausted: bool


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def get_status() -> QuotaStatus:
    with _lock:
        state = _load_state()
        used = state.get(_today_key(), 0)
        remaining = max(0, DAILY_LIMIT - used)
        return QuotaStatus(used=used, limit=DAILY_LIMIT, remaining=remaining, exhausted=remaining == 0)


def record_attempt() -> None:
    """Call once per successful Gemini vision call, to count it against today's local budget."""
    with _lock:
        state = _load_state()
        today = _today_key()
        state = {today: state.get(today, 0)}  # drop older days, keep the file small
        state[today] += 1
        _save_state(state)


def mark_exhausted() -> None:
    """
    Snap today's local counter straight to the daily limit.

    Call this when Gemini itself reports 429 RESOURCE_EXHAUSTED — Google's
    real account-level quota is the actual ceiling, and it can be hit even
    while our local counter still shows room (shared across all traffic
    hitting this API key, including testing done outside this app). Once
    that happens, there's no point letting further requests through only
    to have Gemini reject them too, so this keeps the local counter honest
    until the shared daily quota actually resets.
    """
    with _lock:
        state = _load_state()
        today = _today_key()
        _save_state({today: DAILY_LIMIT})
