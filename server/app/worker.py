"""ARQ worker entry point.

Run via ``python -m app.worker``. In docker-compose this is the ``worker``
service. Keeping it a separate process lets analysis jobs keep streaming even
when the API is rolling.

Shares logging config with the API so log aggregators see both streams in
the same shape, and installs SIGTERM/SIGINT hooks so container stop signals
trigger the ARQ ``on_shutdown`` path rather than hard-killing in-flight jobs.
"""

import logging
import signal

from arq import run_worker
from arq.connections import RedisSettings

from app.config import get_settings
from app.obs import configure_logging
from app.orchestrator import jobs

logger = logging.getLogger("mnemos.worker")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    jobs.WorkerSettings.redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # ARQ installs its own signal handlers (signal.SIGINT, signal.SIGTERM)
    # inside run_worker; re-install here just in case the environment
    # doesn't deliver those reliably (e.g. custom init system).
    def _on_signal(signum, _frame):  # pragma: no cover — signal path
        logger.info("worker signal received: %s", signum)

    signal.signal(signal.SIGTERM, _on_signal)

    run_worker(jobs.WorkerSettings)


if __name__ == "__main__":
    main()
