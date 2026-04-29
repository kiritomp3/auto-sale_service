import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.models import JobState
from app.repositories.job_repository import InMemoryJobRepository
from app.services.card_generation_service import CardGenerationService


class JobService:
    def __init__(
        self,
        job_repository: InMemoryJobRepository,
        card_generation_service: CardGenerationService,
        executor: ThreadPoolExecutor,
        temp_dir: Path,
        output_dir: Path,
    ):
        self._job_repository = job_repository
        self._card_generation_service = card_generation_service
        self._executor = executor
        self._temp_dir = temp_dir
        self._output_dir = output_dir
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _run_job(self, job_id: str, image_path: Path, refinement_prompt: str) -> None:
        self._job_repository.save(JobState(job_id=job_id, status="processing"))
        try:
            result = self._card_generation_service.build_result(image_path, refinement_prompt, job_id)
            result_path = self._output_dir / f"{job_id}_result.json"
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self._job_repository.save(JobState(**result))
        except Exception as error:
            self._job_repository.save(JobState(job_id=job_id, status="failed", error=str(error)))

    def enqueue(self, image_bytes: bytes, image_filename: str, refinement_prompt: str) -> JobState:
        job_id = uuid.uuid4().hex
        extension = Path(image_filename).suffix or ".jpg"
        image_path = self._temp_dir / f"{job_id}{extension}"
        image_path.write_bytes(image_bytes)

        queued_job = JobState(job_id=job_id, status="queued")
        self._job_repository.save(queued_job)
        self._executor.submit(self._run_job, job_id, image_path, refinement_prompt)
        return queued_job

    def get(self, job_id: str) -> JobState | None:
        return self._job_repository.get(job_id)

