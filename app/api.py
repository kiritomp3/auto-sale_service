from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.clients.ozon_seller_client import OzonSellerClient
from app.models import (
    AvitoAccountResponse,
    AvitoAuthLoginRequest,
    AvitoAuthLoginResponse,
    AvitoAuthLogoutResponse,
    AvitoExportFormat,
    AvitoListingDraft,
    AvitoQueuedListing,
    AvitoQueueRequest,
    AvitoQueueResponse,
    AvitoReorderRequest,
    AvitoScheduleResponse,
    AvitoValidationResponse,
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
    TryOnModelsResponse,
)
from app.repositories.metrics_snapshot_repository import RedisMetricsSnapshotRepository
from app.services.job_service import JobService
from app.services.avito_service import AvitoService
from app.services.ozon_analytics_service import OzonAnalyticsService
from app.services.ozon_auth_service import OzonAuthService
from app.services.ozon_draft_service import OzonDraftService
from app.services.ozon_ai_chat_service import OzonAIChatService
from app.services.ozon_insights_service import OzonInsightsService
from app.services.tryon_service import TryOnService


def build_router(
    job_service: JobService,
    ozon_auth_service: OzonAuthService,
    ozon_base_url: str,
    tryon_service: TryOnService | None = None,
    metrics_snapshot_repository: RedisMetricsSnapshotRepository | None = None,
    ozon_insights_service: OzonInsightsService | None = None,
    ozon_ai_chat_service: OzonAIChatService | None = None,
    avito_service: AvitoService | None = None,
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
        size: str = Form(default=""),
        marketplace: str = Form(default="wb"),
        n_cards: int = Form(default=1),
    ) -> CardJobResponse:
        if not image.filename:
            raise HTTPException(status_code=400, detail="Файл изображения обязателен")
        n_cards = 1
        payload = await image.read()
        try:
            job = job_service.enqueue(payload, image.filename, refinement_prompt, size, marketplace, n_cards)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CardJobResponse(job_id=job.job_id, status=job.status)

    @router.get("/crads/{job_id}", response_model=JobState)
    def get_job_result(job_id: str) -> JobState:
        job = job_service.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_id не найден")
        return job

    # ------------------------------------------------------------------
    # Try-on: наложение одежды на модель
    # ------------------------------------------------------------------

    def _require_tryon() -> TryOnService:
        if tryon_service is None:
            raise HTTPException(status_code=503, detail="Сервис try-on недоступен")
        return tryon_service

    @router.get("/tryon/models", response_model=TryOnModelsResponse)
    def list_tryon_models(request: Request) -> TryOnModelsResponse:
        svc = _require_tryon()
        base_url = str(request.base_url).rstrip("/")
        return TryOnModelsResponse(models=svc.list_models(base_url=base_url))

    @router.post("/tryon", response_model=CardJobResponse)
    async def create_tryon(
        garment: UploadFile = File(..., description="Фото одежды"),
        model_id: str = Form(default=""),
        model_image: UploadFile | None = File(default=None),
        marketplace: str = Form(default="wb"),
        prompt: str = Form(default=""),
        n_cards: int = Form(default=1),
    ) -> CardJobResponse:
        svc = _require_tryon()
        if not garment.filename:
            raise HTTPException(status_code=400, detail="Фото одежды обязательно")

        garment_bytes = await garment.read()
        model_bytes = None
        model_filename = None
        if model_image is not None and model_image.filename:
            model_bytes = await model_image.read()
            model_filename = model_image.filename

        try:
            job = svc.enqueue(
                garment_bytes=garment_bytes,
                garment_filename=garment.filename,
                model_id=model_id or None,
                model_bytes=model_bytes,
                model_filename=model_filename,
                marketplace=marketplace,
                extra_prompt=prompt,
                n_images=n_cards,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CardJobResponse(job_id=job.job_id, status=job.status)

    @router.get("/tryon/{job_id}", response_model=JobState)
    def get_tryon_result(job_id: str) -> JobState:
        svc = _require_tryon()
        job = svc.get(job_id)
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

    # ------------------------------------------------------------------
    # Avito: official queue, scheduler and exports
    # ------------------------------------------------------------------

    def _require_avito_service() -> AvitoService:
        if avito_service is None:
            raise HTTPException(status_code=503, detail="Avito integration is not configured")
        return avito_service

    def _get_avito_account_id(token: str = Depends(_get_token)) -> str:
        svc = _require_avito_service()
        session = svc.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Avito session not found or expired")
        return session.account_id

    @router.post("/auth/avito/login", response_model=AvitoAuthLoginResponse)
    def avito_login(payload: AvitoAuthLoginRequest) -> AvitoAuthLoginResponse:
        svc = _require_avito_service()
        return svc.login(
            avito_account_id=payload.avito_account_id,
            avito_access_token=payload.avito_access_token,
            avito_refresh_token=payload.avito_refresh_token,
            account_name=payload.account_name,
        )

    @router.post("/auth/avito/logout", response_model=AvitoAuthLogoutResponse)
    def avito_logout(token: str = Depends(_get_token)) -> AvitoAuthLogoutResponse:
        svc = _require_avito_service()
        svc.logout(token)
        return AvitoAuthLogoutResponse()

    @router.get("/avito/account", response_model=AvitoAccountResponse)
    def get_avito_account(account_id: str = Depends(_get_avito_account_id)) -> AvitoAccountResponse:
        svc = _require_avito_service()
        try:
            return svc.get_account_response(account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Avito account not found") from exc

    @router.post("/avito/listings/validate", response_model=AvitoValidationResponse)
    def validate_avito_listing(payload: AvitoListingDraft) -> AvitoValidationResponse:
        svc = _require_avito_service()
        return svc.validate_draft(payload)

    @router.post("/avito/queue", response_model=AvitoQueueResponse)
    def queue_avito_listings(
        payload: AvitoQueueRequest,
        account_id: str = Depends(_get_avito_account_id),
    ) -> AvitoQueueResponse:
        svc = _require_avito_service()
        try:
            return svc.queue_items(account_id, payload.items)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Avito account not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/avito/schedule", response_model=AvitoScheduleResponse)
    def get_avito_schedule(account_id: str = Depends(_get_avito_account_id)) -> AvitoScheduleResponse:
        svc = _require_avito_service()
        try:
            return svc.get_schedule(account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Avito account not found") from exc

    @router.post("/avito/queue/reorder", response_model=AvitoScheduleResponse)
    def reorder_avito_queue(
        payload: AvitoReorderRequest,
        account_id: str = Depends(_get_avito_account_id),
    ) -> AvitoScheduleResponse:
        svc = _require_avito_service()
        try:
            return svc.reorder(account_id, payload.listing_ids)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Avito listing not found: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/avito/queue/{listing_id}", response_model=AvitoQueuedListing)
    def get_avito_listing(
        listing_id: str,
        account_id: str = Depends(_get_avito_account_id),
    ) -> AvitoQueuedListing:
        svc = _require_avito_service()
        try:
            return svc.get_listing(account_id, listing_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Avito listing not found") from exc

    @router.post("/avito/queue/{listing_id}/cancel", response_model=AvitoQueuedListing)
    def cancel_avito_listing(
        listing_id: str,
        account_id: str = Depends(_get_avito_account_id),
    ) -> AvitoQueuedListing:
        svc = _require_avito_service()
        try:
            return svc.cancel_listing(account_id, listing_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Avito listing not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/avito/queue/{listing_id}/retry", response_model=AvitoQueuedListing)
    def retry_avito_listing(
        listing_id: str,
        account_id: str = Depends(_get_avito_account_id),
    ) -> AvitoQueuedListing:
        svc = _require_avito_service()
        try:
            return svc.retry_listing(account_id, listing_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Avito listing not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/avito/export")
    def export_avito_listings(
        format: AvitoExportFormat = Query(default="csv"),
        account_id: str = Depends(_get_avito_account_id),
    ) -> FileResponse:
        svc = _require_avito_service()
        try:
            path, media_type, filename = svc.export_listings(account_id, format)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Avito account not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(path=path, media_type=media_type, filename=filename)

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
