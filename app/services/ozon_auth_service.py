from __future__ import annotations

import secrets

from app.repositories.ozon_auth_repository import OzonSession, RedisOzonAuthRepository


class OzonAuthService:
    def __init__(self, repository: RedisOzonAuthRepository, session_ttl_seconds: int):
        self._repository = repository
        self._session_ttl_seconds = session_ttl_seconds

    def login(self, client_id: str, api_key: str) -> OzonSession:
        token = secrets.token_urlsafe(48)
        session = OzonSession(
            token=token,
            client_id=client_id,
            api_key=api_key,
            expires_at=self._repository.build_expiry(self._session_ttl_seconds),
        )
        self._repository.save(session, ttl_seconds=self._session_ttl_seconds)
        return session

    def get_session(self, token: str) -> OzonSession | None:
        return self._repository.get_valid(token)

    def logout(self, token: str) -> None:
        self._repository.delete(token)

    @property
    def session_ttl_seconds(self) -> int:
        return self._session_ttl_seconds
