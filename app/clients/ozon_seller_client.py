from __future__ import annotations

from typing import Any

import httpx


class OzonSellerClient:
    def __init__(
        self,
        client_id: str,
        api_key: str,
        base_url: str = "https://api-seller.ozon.ru",
        timeout_seconds: float = 30.0,
    ):
        self._client_id = client_id
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def create_draft_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"items": items}
        return self._post("/v2/product/import", payload)

    def get_analytics_data(
        self,
        date_from: str,
        date_to: str,
        metrics: list[str],
        dimension: list[str],
        filters: list[dict[str, Any]],
        sort: list[dict[str, Any]],
        limit: int = 1000,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._post("/v1/analytics/data", {
            "date_from": date_from,
            "date_to": date_to,
            "metrics": metrics,
            "dimension": dimension,
            "filters": filters,
            "sort": sort,
            "limit": limit,
            "offset": offset,
        })

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "Client-Id": self._client_id,
            "Api-Key": self._api_key,
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)

        response.raise_for_status()
        return response.json()
