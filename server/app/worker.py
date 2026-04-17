"""ARQ worker entry point.

Run via ``python -m app.worker``. In docker-compose this is the ``worker``
service. Keeping it a separate process lets analysis jobs keep streaming even
when the API is rolling.
"""

from arq import run_worker
from arq.connections import RedisSettings

from app.config import get_settings
from app.orchestrator import jobs


def main() -> None:
    settings = get_settings()
    jobs.WorkerSettings.redis_settings = RedisSettings.from_dsn(settings.redis_url)
    run_worker(jobs.WorkerSettings)


if __name__ == "__main__":
    main()
