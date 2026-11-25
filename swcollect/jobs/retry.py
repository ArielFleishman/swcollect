"""Retries for SWIS reads: connection errors, timeouts and 5xx are retried with backoff; 4xx (401/403/400) never."""
from __future__ import annotations

import time

from ..swis.client import SwisError

DELAYS = (1, 3, 9)


def retryable(e: SwisError) -> bool:
    return e.status is None or e.status >= 500


def retry(fn, attempts: int = 3, delays=DELAYS, sleep=time.sleep):
    for i in range(attempts):
        try:
            return fn()
        except SwisError as e:
            if i == attempts - 1 or not retryable(e):
                raise
            sleep(delays[min(i, len(delays) - 1)])
