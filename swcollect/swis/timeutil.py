# Copied from swcheck swcheck/timeutil.py at commit 0958074 (2026-11-18). Local changes: import paths only.
"""Date helpers for SWIS values ("2026-10-25T09:12:33.0570000Z" and friends)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_FRAC = re.compile(r"\.(\d+)")


def parse_dt(v) -> datetime | None:
    """Returns a naive datetime (time-zone dropped). Callers decide whether it is UTC or server-local."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    s = str(v).strip()
    if s.startswith("/Date("):          # legacy WCF JSON
        ms = int(re.findall(r"-?\d+", s)[0])
        return datetime(1970, 1, 1) + timedelta(milliseconds=ms)
    s = s.replace(" ", "T")
    s = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", s)
    s = _FRAC.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fmt_dt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_zero_date(d: datetime | None) -> bool:
    return d is None or d.year < 1901
