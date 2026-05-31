from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import redis

from app.models import OzonAnalyticsResponse


@dataclass
class MetricsSnapshot:
    client_id: str
    date_from: str
    date_to: str
    fetched_at: datetime
    payload: dict


class RedisMetricsSnapshotRepository:
    """Хранит снапшоты аналитики в Redis.

    Нужен для будущего агента, который будет сравнивать метрики
    во времени и реагировать на ухудшение показателей.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        key_prefix: str = "ozon:metrics_snapshot",
        ttl_seconds: int = 90 * 24 * 3600,
    ):
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, client_id: str, date_from: str, date_to: str) -> str:
        return f"{self._key_prefix}:{client_id}:{date_from}:{date_to}"

    def _history_key(self, client_id: str) -> str:
        return f"{self._key_prefix}:history:{client_id}"

    def save(self, client_id: str, analytics: OzonAnalyticsResponse) -> None:
        key = self._key(client_id, analytics.source.date_from, analytics.source.date_to)
        payload = analytics.model_dump(mode="json")
        self._redis.set(key, json.dumps(payload), ex=self._ttl_seconds)

        # Сохраняем ссылку в хронологический список для агента
        history_key = self._history_key(client_id)
        entry = json.dumps({
            "date_from": analytics.source.date_from,
            "date_to": analytics.source.date_to,
            "fetched_at": analytics.fetched_at.isoformat(),
            "snapshot_key": key,
        })
        self._redis.lpush(history_key, entry)
        self._redis.ltrim(history_key, 0, 499)  # храним последние 500 запросов
        self._redis.expire(history_key, self._ttl_seconds)

    def get(self, client_id: str, date_from: str, date_to: str) -> MetricsSnapshot | None:
        key = self._key(client_id, date_from, date_to)
        raw = self._redis.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        return MetricsSnapshot(
            client_id=client_id,
            date_from=date_from,
            date_to=date_to,
            fetched_at=datetime.fromisoformat(payload["fetched_at"]),
            payload=payload,
        )

    def get_history(self, client_id: str, limit: int = 50) -> list[dict]:
        history_key = self._history_key(client_id)
        raw_entries = self._redis.lrange(history_key, 0, limit - 1)
        result = []
        for raw in raw_entries:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                result.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return result
