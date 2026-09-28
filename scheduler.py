"""Standalone scheduler entry point.

Only needed when the web process must not run the scheduler (e.g. when you
scale the web tier to several gunicorn workers and want exactly one billing
process). Set ``SCHEDULER_ENABLED=false`` in ``.env`` and enable
``verzehr-scheduler.service`` instead.

Usage::

    python scheduler.py
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from app import create_app
from app.scheduler import init_scheduler
from app.config import Config, configure_logging

log = logging.getLogger("scheduler")


def main() -> int:
    cfg = Config()
    configure_logging(cfg)
    os.environ.setdefault("SCHEDULER_ENABLED", "true")
    app = create_app(cfg, start_scheduler=False)
    scheduler = init_scheduler(app, start=True)
    log.info("Eigenstaendiger Scheduler gestartet (PID %s)", os.getpid())

    stopping = {"flag": False}

    def _stop(signum, _frame):  # pragma: no cover - signal path
        log.info("Signal %s empfangen, Scheduler wird beendet", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while not stopping["flag"]:
            time.sleep(1)
    finally:
        from app.scheduler import shutdown_scheduler

        shutdown_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
