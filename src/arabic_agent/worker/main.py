"""Worker process entry point.

Run one or more worker processes alongside the API in the queued (cloud)
runtime:

    AAP_EXECUTION_MODE=queued AAP_DATABASE_URL=... AAP_REDIS_URL=... \\
        python -m arabic_agent.worker.main

Each process claims jobs from the shared Redis queue; because leases and run
state live in Postgres, workers can be started, killed and restarted freely
without losing a task.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from ..container import Platform
from ..core.config import Settings
from ..core.errors import ConfigurationError
from ..observability import configure_observability
from ..runs.store import RunStore
from .queue import RedisJobQueue
from .runtime import Worker

logger = logging.getLogger(__name__)


async def _run_worker(settings: Settings) -> None:
    if settings.execution_mode != "queued":
        logger.warning(
            "worker started with execution_mode=%s; set AAP_EXECUTION_MODE=queued",
            settings.execution_mode,
        )
    platform = Platform.build(settings)
    runs: RunStore = platform.runs
    queue = platform.queue
    if not isinstance(queue, RedisJobQueue):
        raise ConfigurationError(
            "the worker requires a Redis job queue; set AAP_REDIS_URL "
            "(or run the inline runtime for single-process local use)"
        )
    worker = Worker(platform=platform, runs=runs, queue=queue, settings=settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.run_forever()
    finally:
        await platform.aclose()


def main() -> None:
    settings = Settings()
    if settings.log_json:
        configure_observability(settings)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    try:
        asyncio.run(_run_worker(settings))
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
