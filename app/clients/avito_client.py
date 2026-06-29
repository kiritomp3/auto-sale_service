from __future__ import annotations

from typing import Any

import httpx


class AvitoClient:
    def __init__(
        self,
        access_token: str,
        base_url: str = "https://api.avito.ru",
        publish_path: str = "/autoload/v2/items",
        timeout_seconds: float = 30.0,
    ):
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._publish_path = publish_path if publish_path.startswith("/") else f"/{publish_path}"
        self._timeout_seconds = timeout_seconds

    def publish_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(self._publish_path, payload)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()
