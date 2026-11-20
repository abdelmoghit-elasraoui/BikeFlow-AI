"""Timestamped progress logging shared by every long-running stage."""

import time
from datetime import datetime, timezone

_STARTED = time.monotonic()


def log(message):
    elapsed = time.monotonic() - _STARTED
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now} UTC +{elapsed:7.1f}s] {message}", flush=True)
