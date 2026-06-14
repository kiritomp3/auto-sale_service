from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis


@dataclass
class AvitoSession:
    token: str  # наш сессионный токен (Bearer для нашего API)
    client_id: str
    client_secret: str
    access_token: str  # Avito OAuth access_token
    access_token_expires_at: datetime
    expires_at: datetime


class RedisAvitoAuthRepository:
    def __init__(self, redis_client: redis.Redis, key_prefix: str = "avito:session"):
        self._redis = redis_client
        self._key_prefix = key_prefix

    def _key(self, token: str) -> str:
        return f"{self._key_prefix}:{token}"

    def save(self, session: AvitoSession, ttl_seconds: int) -> None:
        payload = {
            "token": session.token,
            "client_id": session.client_id,
            "client_secret": session.client_secret,
            "access_token": session.access_token,
            "access_token_expires_at": session.access_token_expires_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
        }
        self._redis.set(self._key(session.token), json.dumps(payload), ex=ttl_seconds)

    def get_valid(self, token: str) -> AvitoSession | None:
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

        return AvitoSession(
            token=data["token"],
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            access_token=data["access_token"],
            access_token_expires_at=datetime.fromisoformat(data["access_token_expires_at"]),
            expires_at=expires_at,
        )

    def delete(self, token: str) -> None:
        self._redis.delete(self._key(token))

    @staticmethod
    def build_expiry(ttl_seconds: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
