import redis

from app.models import JobState


class RedisJobRepository:
    def __init__(self, redis_client: redis.Redis, key_prefix: str = "job", ttl_seconds: int = 30 * 24 * 3600):
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._ttl = ttl_seconds

    def _key(self, job_id: str) -> str:
        return f"{self._key_prefix}:{job_id}"

    def save(self, job: JobState) -> None:
        self._redis.set(self._key(job.job_id), job.model_dump_json(), ex=self._ttl)

    def get(self, job_id: str) -> JobState | None:
        raw = self._redis.get(self._key(job_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return JobState.model_validate_json(raw)

