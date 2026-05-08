from __future__ import annotations

from typing import Any

import httpx

from app.clients.ozon_seller_client import OzonSellerClient


class OzonDraftService:
    def __init__(self, client: OzonSellerClient):
        self._client = client

    def create_drafts(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return self._client.create_draft_items(items)
        except httpx.HTTPStatusError as exc:
            details: str | dict[str, Any]
            try:
                details = exc.response.json()
            except Exception:
                details = exc.response.text
            raise RuntimeError(f"Ozon API error ({exc.response.status_code}): {details}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ошибка сети при обращении к Ozon: {exc}") from exc
