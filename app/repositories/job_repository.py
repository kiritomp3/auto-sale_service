from threading import Lock

from app.models import JobState


class InMemoryJobRepository:
    def __init__(self):
        self._jobs: dict[str, JobState] = {}
        self._lock = Lock()

    def save(self, job: JobState) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

