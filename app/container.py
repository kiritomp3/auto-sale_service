from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import redis

from app.clients.kie_ai_client import KieAIChatClient, KieAIImageClient
from app.config import Settings
from app.repositories.avito_auth_repository import RedisAvitoAuthRepository
from app.repositories.job_repository import RedisJobRepository
from app.repositories.metrics_snapshot_repository import RedisMetricsSnapshotRepository
from app.repositories.ozon_auth_repository import RedisOzonAuthRepository
from app.services.avito_auth_service import AvitoAuthService
from app.services.card_generation_service import CardGenerationService
from app.services.job_service import JobService
from app.services.ozon_auth_service import OzonAuthService
from app.services.tryon_service import TryOnService
from app.services.ozon_ai_chat_service import OzonAIChatService
from app.services.ozon_insights_service import OzonInsightsService


class Container:
    def __init__(self, settings: Settings):
        self.settings = settings

        self.redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=False)
        self.job_repository = RedisJobRepository(
            redis_client=self.redis_client,
            key_prefix=settings.redis_job_key_prefix,
            ttl_seconds=settings.job_ttl_seconds,
        )
        self.kie_ai_chat_client = KieAIChatClient(api_key=settings.kie_ai_api_key)
        self.kie_ai_image_client = KieAIImageClient(api_key=settings.kie_ai_api_key)
        self.card_generation_service = CardGenerationService(
            text_client=self.kie_ai_chat_client,
            image_client=self.kie_ai_image_client,
            output_dir=settings.output_dir,
            image_size=settings.image_size,
        )
        self.executor = ThreadPoolExecutor(max_workers=settings.worker_count)
        self.job_service = JobService(
            job_repository=self.job_repository,
            card_generation_service=self.card_generation_service,
            executor=self.executor,
            temp_dir=settings.temp_dir,
            output_dir=settings.output_dir,
        )

        self.ozon_auth_repository = RedisOzonAuthRepository(
            redis_client=self.redis_client,
            key_prefix=settings.redis_ozon_session_prefix,
        )
        self.ozon_auth_service = OzonAuthService(
            repository=self.ozon_auth_repository,
            session_ttl_seconds=settings.ozon_session_ttl_seconds,
        )

        self.avito_auth_repository = RedisAvitoAuthRepository(
            redis_client=self.redis_client,
            key_prefix=settings.redis_avito_session_prefix,
        )
        self.avito_auth_service = AvitoAuthService(
            repository=self.avito_auth_repository,
            session_ttl_seconds=settings.avito_session_ttl_seconds,
            base_url=settings.avito_base_url,
        )

        self.tryon_service = TryOnService(
            job_repository=self.job_repository,
            image_client=self.kie_ai_image_client,
            executor=self.executor,
            temp_dir=settings.temp_dir,
            output_dir=settings.output_dir,
            models_dir=settings.models_dir,
        )
        self.metrics_snapshot_repository = RedisMetricsSnapshotRepository(
            redis_client=self.redis_client,
            key_prefix=settings.redis_metrics_snapshot_prefix,
            ttl_seconds=settings.metrics_snapshot_ttl_seconds,
        )
        self.ozon_insights_service = OzonInsightsService(
            snapshot_repository=self.metrics_snapshot_repository,
        )
        self.ozon_ai_chat_service = OzonAIChatService(
            kie_ai_client=self.kie_ai_chat_client,
            insights_service=self.ozon_insights_service,
        )
