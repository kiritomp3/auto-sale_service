from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    ozon_base_url: str = "https://api-seller.ozon.ru"
    ozon_session_ttl_seconds: int = 60 * 60 * 12
    redis_url: str = "redis://localhost:6379/0"
    redis_ozon_session_prefix: str = "ozon:session"
    output_dir: Path = Path("output")
    temp_dir: Path = Path("tmp")
    image_model: str = "gpt-image-1"
    text_model: str = "gpt-5"
    worker_count: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY не найден в переменных окружения")

        ttl_raw = os.getenv("OZON_SESSION_TTL_SECONDS", str(60 * 60 * 12))
        try:
            ttl_value = int(ttl_raw)
        except ValueError as exc:
            raise RuntimeError("OZON_SESSION_TTL_SECONDS должен быть целым числом") from exc
        if ttl_value <= 0:
            raise RuntimeError("OZON_SESSION_TTL_SECONDS должен быть больше 0")

        return cls(
            openai_api_key=api_key,
            ozon_base_url=os.getenv("OZON_BASE_URL", "https://api-seller.ozon.ru"),
            ozon_session_ttl_seconds=ttl_value,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            redis_ozon_session_prefix=os.getenv("REDIS_OZON_SESSION_PREFIX", "ozon:session"),
        )
