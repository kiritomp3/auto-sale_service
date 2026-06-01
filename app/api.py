from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.clients.ozon_seller_client import OzonSellerClient
from app.models import (
    CardJobResponse,
    JobState,
    OzonAIChatRequest,
    OzonAIChatResponse,
    OzonAnalyticsRequest,
    OzonAnalyticsResponse,
    OzonAuthLoginRequest,
    OzonAuthLoginResponse,
    OzonAuthLogoutResponse,
    OzonCompareRequest,
    OzonDraftCreateRequest,
    OzonDraftCreateResponse,
    OzonHistoryResponse,
    OzonHistoryEntry,
    OzonInsightsResponse,
)
from app.repositories.metrics_snapshot_repository import RedisMetricsSnapshotRepository
from app.services.job_service import JobService
from app.services.ozon_analytics_service import OzonAnalyticsService
from app.services.ozon_auth_service import OzonAuthService
from app.services.ozon_draft_service import OzonDraftService
from app.services.ozon_ai_chat_service import OzonAIChatService
from app.services.ozon_insights_service import OzonInsightsService


def build_router(
    job_service: JobService,
    ozon_auth_service: OzonAuthService,
    ozon_base_url: str,
    metrics_snapshot_repository: RedisMetricsSnapshotRepository | None = None,
    ozon_insights_service: OzonInsightsService | None = None,
    ozon_ai_chat_service: OzonAIChatService | None = None,
) -> APIRouter:
    router = APIRouter()
    security = HTTPBearer(auto_error=False)

    def _get_token(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Требуется Bearer токен")
        return credentials.credentials

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/cards", response_model=CardJobResponse)
    async def create_cards(
        image: UploadFile = File(...),
        refinement_prompt: str = Form(default=""),
    ) -> CardJobResponse:
        if not image.filename:
            raise HTTPException(status_code=400, detail="Файл изображения обязателен")
        payload = await image.read()
        job = job_service.enqueue(payload, image.filename, refinement_prompt)
        return CardJobResponse(job_id=job.job_id, status=job.status)

    @router.get("/crads/{job_id}", response_model=JobState)
    def get_job_result(job_id: str) -> JobState:
        job = job_service.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_id не найден")
        return job

    @router.post("/auth/ozon/login", response_model=OzonAuthLoginResponse)
    def ozon_login(payload: OzonAuthLoginRequest) -> OzonAuthLoginResponse:
        session = ozon_auth_service.login(
            client_id=payload.ozon_client_id,
            api_key=payload.ozon_api_key,
        )
        return OzonAuthLoginResponse(
            access_token=session.token,
            expires_in=ozon_auth_service.session_ttl_seconds,
            expires_at=session.expires_at,
        )

    @router.post("/auth/ozon/logout", response_model=OzonAuthLogoutResponse)
    def ozon_logout(token: str = Depends(_get_token)) -> OzonAuthLogoutResponse:
        ozon_auth_service.logout(token)
        return OzonAuthLogoutResponse()

    @router.post("/ozon/drafts", response_model=OzonDraftCreateResponse)
    def create_ozon_drafts(
        payload: OzonDraftCreateRequest,
        token: str = Depends(_get_token),
    ) -> OzonDraftCreateResponse:
        session = ozon_auth_service.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")

        client = OzonSellerClient(
            client_id=session.client_id,
            api_key=session.api_key,
            base_url=ozon_base_url,
        )
        draft_service = OzonDraftService(client=client)

        try:
            result = draft_service.create_drafts(payload.items)
            return OzonDraftCreateResponse(result=result)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/ozon/analytics", response_model=OzonAnalyticsResponse)
    def get_ozon_analytics(
        payload: OzonAnalyticsRequest,
        token: str = Depends(_get_token),
    ) -> OzonAnalyticsResponse:
        session = ozon_auth_service.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")

        client = OzonSellerClient(
            client_id=session.client_id,
            api_key=session.api_key,
            base_url=ozon_base_url,
        )
        analytics_service = OzonAnalyticsService(client=client)

        try:
            result = analytics_service.fetch(
                date_from=payload.date_from,
                date_to=payload.date_to,
                dimension=payload.dimension,
                sku=payload.sku,
                metric_ids=payload.metrics or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if metrics_snapshot_repository is not None:
            try:
                metrics_snapshot_repository.save(session.client_id, result)
            except Exception:
                pass  # снапшот — некритичная операция

        return result

    # ------------------------------------------------------------------
    # Insights / analytics comparison
    # ------------------------------------------------------------------

    def _require_insights_service() -> OzonInsightsService:
        if ozon_insights_service is None:
            raise HTTPException(status_code=503, detail="Сервис инсайтов недоступен")
        return ozon_insights_service

    @router.get("/ozon/analytics/history", response_model=OzonHistoryResponse)
    def get_analytics_history(
        limit: int = 50,
        token: str = Depends(_get_token),
    ) -> OzonHistoryResponse:
        session = ozon_auth_service.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")
        svc = _require_insights_service()
        raw = svc.get_history(session.client_id, limit=min(limit, 200))
        entries = [
            OzonHistoryEntry(
                date_from=e.get("date_from", ""),
                date_to=e.get("date_to", ""),
                fetched_at=e.get("fetched_at", ""),
                snapshot_key=e.get("snapshot_key", ""),
            )
            for e in raw
        ]
        return OzonHistoryResponse(
            client_id=session.client_id,
            snapshots=entries,
            total=len(entries),
        )

    @router.get("/ozon/analytics/insights", response_model=OzonInsightsResponse)
    def get_analytics_insights(
        token: str = Depends(_get_token),
    ) -> OzonInsightsResponse:
        """Авто-анализ: берёт последний снапшот из Redis и сравнивает с предыдущим периодом."""
        session = ozon_auth_service.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")
        svc = _require_insights_service()
        try:
            return svc.get_insights(session.client_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/ozon/analytics/compare", response_model=OzonInsightsResponse)
    def compare_analytics(
        payload: OzonCompareRequest,
        token: str = Depends(_get_token),
    ) -> OzonInsightsResponse:
        """
        Сравнивает два периода. Если данные не в кэше — автоматически запрашивает у Ozon API.
        Предыдущий период вычисляется автоматически (той же длины), если не указан явно.
        """
        session = ozon_auth_service.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")

        svc = _require_insights_service()
        client = OzonSellerClient(
            client_id=session.client_id,
            api_key=session.api_key,
            base_url=ozon_base_url,
        )
        analytics_service = OzonAnalyticsService(client=client)

        try:
            return svc.compare(
                client_id=session.client_id,
                current_date_from=payload.current_date_from,
                current_date_to=payload.current_date_to,
                previous_date_from=payload.previous_date_from,
                previous_date_to=payload.previous_date_to,
                sku=payload.sku,
                analytics_service=analytics_service,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/ozon/analytics/chat", response_model=OzonAIChatResponse)
    def analytics_chat(
        payload: OzonAIChatRequest,
        token: str = Depends(_get_token),
    ) -> OzonAIChatResponse:
        """
        AI-чат по метрикам. GPT-4o-mini читает данные за период и отвечает на вопрос.
        Если question пустой — даёт общий анализ с конкретными рекомендациями.
        Автоматически подтягивает данные из Ozon API если нет кэша.
        """
        if ozon_ai_chat_service is None:
            raise HTTPException(status_code=503, detail="AI чат недоступен: не настроен OPENAI_API_KEY")

        session = ozon_auth_service.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")

        client = OzonSellerClient(
            client_id=session.client_id,
            api_key=session.api_key,
            base_url=ozon_base_url,
        )
        analytics_service = OzonAnalyticsService(client=client)

        try:
            return ozon_ai_chat_service.chat(
                client_id=session.client_id,
                current_date_from=payload.current_date_from,
                current_date_to=payload.current_date_to,
                question=payload.question,
                sku=payload.sku,
                analytics_service=analytics_service,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
