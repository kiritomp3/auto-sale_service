from __future__ import annotations

from typing import Any

import httpx


class AvitoClient:
    """
    Клиент Avito API (https://developers.avito.ru).

    Авторизация — OAuth2 client_credentials: пара client_id / client_secret
    обменивается на короткоживущий Bearer access_token.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api.avito.ru",
        timeout_seconds: float = 30.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def fetch_token(self) -> dict[str, Any]:
        """Обменивает client_id/client_secret на access_token.

        Возвращает тело ответа Avito: {access_token, expires_in, token_type}.
        """
        url = f"{self._base_url}/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # Account / listings
    # ------------------------------------------------------------------
    def get_self(self, access_token: str) -> dict[str, Any]:
        """Информация о текущем аккаунте продавца."""
        return self._get("/core/v1/accounts/self", access_token)

    def list_items(
        self,
        access_token: str,
        per_page: int = 25,
        page: int = 1,
        status: str = "active",
    ) -> dict[str, Any]:
        """Список объявлений продавца (страница)."""
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        if status:
            params["status"] = status
        return self._get("/core/v1/items", access_token, params=params)

    def get_balance(self, access_token: str, user_id: int) -> dict[str, Any]:
        """Баланс кошелька продавца."""
        return self._get(f"/core/v1/accounts/{user_id}/balance/", access_token)

    # ------------------------------------------------------------------
    def _get(
        self,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
