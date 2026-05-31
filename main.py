import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import build_router
from app.config import Settings
from app.container import Container

logger = logging.getLogger(__name__)


async def _cleanup_loop(output_dir: Path, temp_dir: Path, job_ttl: int, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        now = time.time()
        for path, ttl in ((output_dir, job_ttl), (temp_dir, 24 * 3600)):
            if not path.exists():
                continue
            for f in path.iterdir():
                if f.is_file() and now - f.stat().st_mtime > ttl:
                    try:
                        f.unlink()
                    except OSError:
                        logger.warning("Не удалось удалить %s", f)


settings = Settings.from_env()
container = Container(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(
        _cleanup_loop(
            output_dir=settings.output_dir,
            temp_dir=settings.temp_dir,
            job_ttl=settings.job_ttl_seconds,
            interval=settings.cleanup_interval_seconds,
        )
    )
    yield
    task.cancel()


app = FastAPI(
    title="WB Cards Service",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
app.mount("/output", StaticFiles(directory=settings.output_dir), name="output")
app.include_router(
    build_router(
        job_service=container.job_service,
        ozon_auth_service=container.ozon_auth_service,
        ozon_base_url=settings.ozon_base_url,
        metrics_snapshot_repository=container.metrics_snapshot_repository,
    )
)
