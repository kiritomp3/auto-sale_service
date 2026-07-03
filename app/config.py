from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

DEFAULT_IMAGE_SIZE = "1024x1536"
SUPPORTED_IMAGE_SIZES = frozenset({"1024x1024", "1024x1536", "1536x1024", "auto"})

DEFAULT_MARKETPLACE = "wb"
SUPPORTED_MARKETPLACES = frozenset({"wb", "ozon", "avito", "yandex_market", "megamarket"})
MARKETPLACE_ALIASES = {
    "wildberries": "wb",
    "wild_berries": "wb",
    "yandex": "yandex_market",
    "yandexmarket": "yandex_market",
    "yandex_marketplace": "yandex_market",
    "ymarket": "yandex_market",
    "mega": "megamarket",
    "mega_market": "megamarket",
    "sber": "megamarket",
    "sbermarket": "megamarket",
    "sbermegamarket": "megamarket",
    "sber_megamarket": "megamarket",
    "sber_mega_market": "megamarket",
}


def normalize_image_size(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in SUPPORTED_IMAGE_SIZES else DEFAULT_IMAGE_SIZE


def normalize_marketplace(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    normalized = MARKETPLACE_ALIASES.get(normalized, normalized)
    return normalized if normalized in SUPPORTED_MARKETPLACES else DEFAULT_MARKETPLACE


@dataclass(frozen=True)
class Settings:
    kie_ai_api_key: str
    ozon_base_url: str = "https://api-seller.ozon.ru"
    ozon_session_ttl_seconds: int = 60 * 60 * 12
    avito_base_url: str = "https://api.avito.ru"
    avito_publish_path: str = "/autoload/v2/items"
    avito_session_ttl_seconds: int = 60 * 60 * 12
    avito_publish_interval_seconds: int = 3700
    avito_scheduler_interval_seconds: int = 30
    redis_url: str = "redis://localhost:6379/0"
    redis_ozon_session_prefix: str = "ozon:session"
    redis_avito_prefix: str = "avito"
    redis_job_key_prefix: str = "job"
    redis_metrics_snapshot_prefix: str = "ozon:metrics_snapshot"
    metrics_snapshot_ttl_seconds: int = 90 * 24 * 3600
    job_ttl_seconds: int = 30 * 24 * 3600
    cleanup_interval_seconds: int = 30 * 24 * 3600
    output_dir: Path = Path("output")
    temp_dir: Path = Path("tmp")
    models_dir: Path = Path("models")
    image_size: str = DEFAULT_IMAGE_SIZE
    worker_count: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        kie_ai_key = os.getenv("KIE_AI_API_KEY")
        if not kie_ai_key:
            raise RuntimeError("KIE_AI_API_KEY не найден в переменных окружения")

        ttl_raw = os.getenv("OZON_SESSION_TTL_SECONDS", str(60 * 60 * 12))
        try:
            ttl_value = int(ttl_raw)
        except ValueError as exc:
            raise RuntimeError("OZON_SESSION_TTL_SECONDS должен быть целым числом") from exc
        if ttl_value <= 0:
            raise RuntimeError("OZON_SESSION_TTL_SECONDS должен быть больше 0")

        avito_ttl_raw = os.getenv("AVITO_SESSION_TTL_SECONDS", str(60 * 60 * 12))
        try:
            avito_ttl_value = int(avito_ttl_raw)
        except ValueError as exc:
            raise RuntimeError("AVITO_SESSION_TTL_SECONDS must be an integer") from exc
        if avito_ttl_value <= 0:
            raise RuntimeError("AVITO_SESSION_TTL_SECONDS must be greater than 0")

        avito_interval_raw = os.getenv("AVITO_PUBLISH_INTERVAL_SECONDS", "3700")
        try:
            avito_interval_value = int(avito_interval_raw)
        except ValueError as exc:
            raise RuntimeError("AVITO_PUBLISH_INTERVAL_SECONDS must be an integer") from exc
        if avito_interval_value < 3700:
            raise RuntimeError("AVITO_PUBLISH_INTERVAL_SECONDS cannot be lower than 3700")

        avito_scheduler_interval_raw = os.getenv("AVITO_SCHEDULER_INTERVAL_SECONDS", "30")
        try:
            avito_scheduler_interval_value = int(avito_scheduler_interval_raw)
        except ValueError as exc:
            raise RuntimeError("AVITO_SCHEDULER_INTERVAL_SECONDS must be an integer") from exc
        if avito_scheduler_interval_value <= 0:
            raise RuntimeError("AVITO_SCHEDULER_INTERVAL_SECONDS must be greater than 0")

        return cls(
            kie_ai_api_key=kie_ai_key,
            ozon_base_url=os.getenv("OZON_BASE_URL", "https://api-seller.ozon.ru"),
            ozon_session_ttl_seconds=ttl_value,
            avito_base_url=os.getenv("AVITO_BASE_URL", "https://api.avito.ru"),
            avito_publish_path=os.getenv("AVITO_PUBLISH_PATH", "/autoload/v2/items"),
            avito_session_ttl_seconds=avito_ttl_value,
            avito_publish_interval_seconds=avito_interval_value,
            avito_scheduler_interval_seconds=avito_scheduler_interval_value,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            redis_ozon_session_prefix=os.getenv("REDIS_OZON_SESSION_PREFIX", "ozon:session"),
            redis_avito_prefix=os.getenv("REDIS_AVITO_PREFIX", "avito"),
            image_size=normalize_image_size(os.getenv("GENERATION_IMAGE_SIZE") or os.getenv("IMAGE_SIZE")),
        )
