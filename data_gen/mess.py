"""Shared helpers: timestamps, warehouse-sync lag (late arrival), id helpers."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import numpy as np

from .config import EXPORT_AT, WINDOW_START

UTC = timezone.utc
WEEK0 = datetime.combine(WINDOW_START, time(0), tzinfo=UTC)
WEEK0_EPOCH = WEEK0.timestamp()
DOW_P = np.array([.20, .21, .21, .20, .16, .01, .01])        # business-week activity profile


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_naive(dt: datetime, tz_off: float) -> str:
    """Local wall-clock time with NO timezone marker (a classic bad SDK)."""
    return (dt.astimezone(UTC) + timedelta(hours=tz_off)).strftime("%Y-%m-%dT%H:%M:%S")


def at(d: date, hour: float = 12.0, tz_off: float = 0.0) -> datetime:
    """A UTC datetime for local `hour` on date `d` in a timezone offset by tz_off hours."""
    return datetime.combine(d, time(0), tzinfo=UTC) + timedelta(hours=hour - tz_off)


def business_times(rng, n: int, week: int, tz_off: float) -> np.ndarray:
    """n epoch-second timestamps (UTC) inside window week `week`, skewed to local business hours."""
    dow = rng.choice(7, size=n, p=DOW_P)
    hour = np.clip(rng.normal(11.5, 3.0, n), 6, 22)
    return WEEK0_EPOCH + week * 7 * 86400 + dow * 86400 + (hour - tz_off) * 3600


def from_epoch(x: float) -> datetime:
    return datetime.fromtimestamp(x, tz=UTC)


def sync_time(rng, avail: datetime, p_late: float = .03, late_days=(3, 30),
              p_backfill: float = 0.0, backfill_days=(15, 90)) -> datetime:
    """When a record reaches the warehouse.

    Normal: within ~a day (hourly/daily syncs). Late: a few days to weeks (retries, bulk
    imports, offline mobile SDKs). Backfill: a long tail of very old data landing at once.
    """
    r = rng.random()
    if r < p_backfill:
        lag = timedelta(days=float(rng.uniform(*backfill_days)))
    elif r < p_backfill + p_late:
        lag = timedelta(days=float(rng.uniform(*late_days)))
    else:
        lag = timedelta(hours=float(rng.exponential(5.0)) + .1)
    return avail + lag


def arrived(dt: datetime) -> bool:
    """Has this record reached the warehouse by the export cutoff?"""
    return dt <= EXPORT_AT


def pick(rng, seq, weights=None):
    return seq[int(rng.choice(len(seq), p=weights))]
