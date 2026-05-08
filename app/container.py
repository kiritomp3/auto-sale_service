from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import redis

from app.clients.openai_client import OpenAIClient
from app.config import Settings
from app.repositories.job_repository import InMemoryJobRepository
from app.repositories.ozon_auth_repository import RedisOzonAuthRepository
from app.services.card_generation_service import CardGenerationService
from app.services.job_service import JobService
from app.services.ozon_auth_service import OzonAuthService


class Container:
    def __init__(self, settings: Settings):
        self.settings = settings

        self.job_repository = InMemoryJobRepository()
        self.openai_client = OpenAIClient(
            api_key=settings.openai_api_key,
            text_model=settings.text_model,
            image_model=settings.image_model,
        )
        self.card_generation_service = CardGenerationService(
            client=self.openai_client,
            output_dir=settings.output_dir,
        )
        self.executor = ThreadPoolExecutor(max_workers=settings.worker_count)
        self.job_service = JobService(
            job_repository=self.job_repository,
            card_generation_service=self.card_generation_service,
            executor=self.executor,
            temp_dir=settings.temp_dir,
            output_dir=settings.output_dir,
        )

        self.redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=False)
        self.ozon_auth_repository = RedisOzonAuthRepository(
            redis_client=self.redis_client,
            key_prefix=settings.redis_ozon_session_prefix,
        )
        self.ozon_auth_service = OzonAuthService(
            repository=self.ozon_auth_repository,
            session_ttl_seconds=settings.ozon_session_ttl_seconds,
        )
