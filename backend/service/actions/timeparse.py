"""
service/actions/timeparse.py

Reading a time out of whatever the model returned.

A model was being asked to normalise "9 PM" into "21:00". That is a
deterministic string transformation, and asking a 14B model to do it
reliably is the wrong division of labour — it returned "today at 9 PM"
verbatim and the schema rejected it.

The model's job is understanding INTENT: that this is a schedule, for this
suite, happening once. Turning the words into a timestamp is code's job,
and code can do it exactly.

So this accepts whatever comes back and extracts what it can:

    "21:00"           -> 21:00
    "9 PM"            -> 21:00
    "today at 9 PM"   -> 21:00, day=today
    "tomorrow 6am"    -> 06:00, day=tomorrow
    "6"               -> 06:00
    "half past nine"  -> None, and the caller asks
"""

import re
from typing import Optional


def parse_time(value: str) -> Optional[str]:
    """
    Pull a 24-hour HH:MM out of a phrase.

    Args:
        value: Anything the model returned for a time.

    Returns:
        "HH:MM", or None when no time can be read.
    """
    if not value:
        return None
    t = str(value).strip().lower()

    # 21:00, 9:30, 09:05 — with an optional am/pm after it
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        hour = _apply_meridiem(hour, m.group(3))
        return _fmt(hour, minute)

    # 9pm, 9 pm, 6am
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        hour = _apply_meridiem(int(m.group(1)), m.group(2))
        return _fmt(hour, 0)

    # bare number, e.g. "at 6" — treated as 24-hour
    m = re.search(r"\bat\s+(\d{1,2})\b", t) or re.fullmatch(r"\s*(\d{1,2})\s*", t)
    if m:
        return _fmt(int(m.group(1)), 0)

    # words people actually use
    for phrase, hhmm in (("midnight", "00:00"), ("noon", "12:00"),
                         ("midday", "12:00"), ("tonight", "21:00")):
        if phrase in t:
            return hhmm

    return None


def parse_day(*values: str) -> Optional[str]:
    """
    Find "today" or "tomorrow" anywhere in what the model returned.

    Checked across several fields because the model puts it wherever it
    likes — sometimes in `day`, sometimes buried in `time`.
    """
    joined = " ".join(str(v or "").lower() for v in values)
    if "tomorrow" in joined:
        return "tomorrow"
    if "today" in joined or "tonight" in joined:
        return "today"
    return None


def _apply_meridiem(hour: int, meridiem: Optional[str]) -> int:
    if meridiem == "pm" and hour < 12:
        return hour + 12
    if meridiem == "am" and hour == 12:
        return 0
    return hour


def _fmt(hour: int, minute: int) -> Optional[str]:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"
