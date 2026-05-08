from fastapi import FastAPI

from app.api import build_router
from app.config import Settings
from app.container import Container


settings = Settings.from_env()
container = Container(settings)

app = FastAPI(
    title="WB Cards Service",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
app.include_router(
    build_router(
        job_service=container.job_service,
        ozon_auth_service=container.ozon_auth_service,
        ozon_base_url=settings.ozon_base_url,
    )
)
