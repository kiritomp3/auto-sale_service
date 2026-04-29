from concurrent.futures import ThreadPoolExecutor

from app.clients.openai_client import OpenAIClient
from app.config import Settings
from app.repositories.job_repository import InMemoryJobRepository
from app.services.card_generation_service import CardGenerationService
from app.services.job_service import JobService


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

