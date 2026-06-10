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

    def list_postings(
        self,
        schema: str,
        since: str,
        to: str,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Список отправлений FBO или FBS с пагинацией по cursor."""
        if schema == "fbo":
            payload: dict[str, Any] = {
                "dir": "asc",
                "filter": {"since": since, "to": to, "status": ""},
                "limit": limit,
                "translit": False,
                "with": {"analytics_data": False, "financial_data": False},
            }
            if cursor:
                payload["cursor"] = cursor
            return self._post("/v3/posting/fbo/list", payload)
        else:
            payload = {
                "dir": "asc",
                "filter": {"since": since, "to": to, "status": ""},
                "limit": limit,
                "offset": 0,
                "translit": False,
                "with": {"analytics_data": False, "financial_data": False},
            }
            resp = self._post("/v3/posting/fbs/list", payload)
            # FBS возвращает {result: {postings, has_next}} — нормализуем
            result = resp.get("result") or resp
            return {
                "postings": result.get("postings") or [],
                "has_next": result.get("has_next", False),
                "cursor": None,
            }

    def list_finance_transactions(
        self,
        date_from: str,
        date_to: str,
        page: int = 1,
        page_size: int = 1000,
    ) -> dict[str, Any]:
        return self._post("/v3/finance/transaction/list", {
            "filter": {
                "date": {"from": date_from, "to": date_to},
                "transaction_type": "",
            },
            "page": page,
            "page_size": page_size,
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
