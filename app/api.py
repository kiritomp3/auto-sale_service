from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.clients.avito_client import AvitoClient
from app.clients.ozon_seller_client import OzonSellerClient
from app.models import (
    AvitoAccountResponse,
    AvitoAuthLoginRequest,
    AvitoAuthLoginResponse,
    AvitoAuthLogoutResponse,
    AvitoItemsResponse,
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
from app.services.avito_auth_service import AvitoAuthService
from app.services.job_service import JobService
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
    avito_auth_service: AvitoAuthService | None = None,
    avito_base_url: str = "https://api.avito.ru",
    tryon_service: TryOnService | None = None,
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
        size: str = Form(default=""),
        marketplace: str = Form(default="wb"),
        n_cards: int = Form(default=3),
    ) -> CardJobResponse:
        if not image.filename:
            raise HTTPException(status_code=400, detail="Файл изображения обязателен")
        if not 1 <= n_cards <= 6:
            raise HTTPException(status_code=400, detail="Количество карточек должно быть от 1 до 6")
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
    # Avito marketplace
    # ------------------------------------------------------------------

    def _require_avito() -> AvitoAuthService:
        if avito_auth_service is None:
            raise HTTPException(status_code=503, detail="Интеграция Avito недоступна")
        return avito_auth_service

    @router.post("/auth/avito/login", response_model=AvitoAuthLoginResponse)
    def avito_login(payload: AvitoAuthLoginRequest) -> AvitoAuthLoginResponse:
        svc = _require_avito()
        try:
            session = svc.login(
                client_id=payload.avito_client_id,
                client_secret=payload.avito_client_secret,
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return AvitoAuthLoginResponse(
            access_token=session.token,
            expires_in=svc.session_ttl_seconds,
            expires_at=session.expires_at,
        )

    @router.post("/auth/avito/logout", response_model=AvitoAuthLogoutResponse)
    def avito_logout(token: str = Depends(_get_token)) -> AvitoAuthLogoutResponse:
        svc = _require_avito()
        svc.logout(token)
        return AvitoAuthLogoutResponse()

    @router.get("/avito/account", response_model=AvitoAccountResponse)
    def avito_account(token: str = Depends(_get_token)) -> AvitoAccountResponse:
        svc = _require_avito()
        session = svc.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")
        client = AvitoClient(
            client_id=session.client_id,
            client_secret=session.client_secret,
            base_url=avito_base_url,
        )
        try:
            account = client.get_self(svc.get_access_token(session))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Avito API error: {exc}") from exc
        return AvitoAccountResponse(account=account)

    @router.get("/avito/items", response_model=AvitoItemsResponse)
    def avito_items(
        per_page: int = 25,
        page: int = 1,
        status: str = "active",
        token: str = Depends(_get_token),
    ) -> AvitoItemsResponse:
        svc = _require_avito()
        session = svc.get_session(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Сессия не найдена или истекла")
        client = AvitoClient(
            client_id=session.client_id,
            client_secret=session.client_secret,
            base_url=avito_base_url,
        )
        try:
            data = client.list_items(
                svc.get_access_token(session),
                per_page=min(per_page, 100),
                page=page,
                status=status,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Avito API error: {exc}") from exc
        return AvitoItemsResponse(
            items=data.get("resources") or data.get("items") or [],
            meta=data.get("meta", {}),
        )

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
