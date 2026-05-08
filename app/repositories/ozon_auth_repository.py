from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis


@dataclass
class OzonSession:
    token: str
    client_id: str
    api_key: str
    expires_at: datetime


class RedisOzonAuthRepository:
    def __init__(self, redis_client: redis.Redis, key_prefix: str = "ozon:session"):
        self._redis = redis_client
        self._key_prefix = key_prefix

    def _key(self, token: str) -> str:
        return f"{self._key_prefix}:{token}"

    def save(self, session: OzonSession, ttl_seconds: int) -> None:
        payload = {
            "token": session.token,
            "client_id": session.client_id,
            "api_key": session.api_key,
            "expires_at": session.expires_at.isoformat(),
        }
        self._redis.set(self._key(session.token), json.dumps(payload), ex=ttl_seconds)

    def get_valid(self, token: str) -> OzonSession | None:
        raw = self._redis.get(self._key(token))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        data = json.loads(raw)
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at <= datetime.now(timezone.utc):
            self.delete(token)
            return None

        return OzonSession(
            token=data["token"],
            client_id=data["client_id"],
            api_key=data["api_key"],
            expires_at=expires_at,
        )

    def delete(self, token: str) -> None:
        self._redis.delete(self._key(token))

    @staticmethod
    def build_expiry(ttl_seconds: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
