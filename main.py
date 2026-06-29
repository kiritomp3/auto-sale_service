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
    cleanup_task = asyncio.create_task(
        _cleanup_loop(
            output_dir=settings.output_dir,
            temp_dir=settings.temp_dir,
            job_ttl=settings.job_ttl_seconds,
            interval=settings.cleanup_interval_seconds,
        )
    )
    avito_scheduler_task = asyncio.create_task(container.avito_service.run_scheduler_loop())
    yield
    cleanup_task.cancel()
    avito_scheduler_task.cancel()


app = FastAPI(
    title="WB Cards Service",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
settings.models_dir.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=settings.output_dir), name="output")
app.mount("/models", StaticFiles(directory=settings.models_dir), name="models")
app.include_router(
    build_router(
        job_service=container.job_service,
        ozon_auth_service=container.ozon_auth_service,
        ozon_base_url=settings.ozon_base_url,
        tryon_service=container.tryon_service,
        metrics_snapshot_repository=container.metrics_snapshot_repository,
        ozon_insights_service=container.ozon_insights_service,
        ozon_ai_chat_service=container.ozon_ai_chat_service,
        avito_service=container.avito_service,
    )
)
