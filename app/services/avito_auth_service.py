from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import httpx

from app.clients.avito_client import AvitoClient
from app.repositories.avito_auth_repository import AvitoSession, RedisAvitoAuthRepository


class AvitoAuthService:
    """Логин/логаут для Avito.

    Пользователь передаёт client_id + client_secret. Сервис обменивает их на
    Avito access_token, валидируя тем самым креды, и сохраняет сессию в Redis.
    Avito access_token живёт ~24ч и при необходимости перевыпускается.
    """

    def __init__(
        self,
        repository: RedisAvitoAuthRepository,
        session_ttl_seconds: int,
        base_url: str = "https://api.avito.ru",
    ):
        self._repository = repository
        self._session_ttl_seconds = session_ttl_seconds
        self._base_url = base_url

    def login(self, client_id: str, client_secret: str) -> AvitoSession:
        client = AvitoClient(client_id=client_id, client_secret=client_secret, base_url=self._base_url)
        try:
            token_data = client.fetch_token()
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"Avito отклонил учётные данные ({exc.response.status_code})") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ошибка сети при обращении к Avito: {exc}") from exc

        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("Avito не вернул access_token")
        expires_in = int(token_data.get("expires_in", 86400))

        session = AvitoSession(
            token=secrets.token_urlsafe(48),
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
            access_token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            expires_at=self._repository.build_expiry(self._session_ttl_seconds),
        )
        self._repository.save(session, ttl_seconds=self._session_ttl_seconds)
        return session

    def get_session(self, token: str) -> AvitoSession | None:
        return self._repository.get_valid(token)

    def get_access_token(self, session: AvitoSession) -> str:
        """Возвращает действующий Avito access_token, перевыпуская при истечении."""
        skew = timedelta(seconds=60)
        if session.access_token_expires_at - skew > datetime.now(timezone.utc):
            return session.access_token

        client = AvitoClient(
            client_id=session.client_id,
            client_secret=session.client_secret,
            base_url=self._base_url,
        )
        token_data = client.fetch_token()
        access_token = token_data.get("access_token")
        if not access_token:
            raise RuntimeError("Avito не вернул access_token при обновлении")
        expires_in = int(token_data.get("expires_in", 86400))

        session.access_token = access_token
        session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        self._repository.save(session, ttl_seconds=self._session_ttl_seconds)
        return access_token

    def logout(self, token: str) -> None:
        self._repository.delete(token)

    @property
    def session_ttl_seconds(self) -> int:
        return self._session_ttl_seconds
