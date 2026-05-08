from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.clients.ozon_seller_client import OzonSellerClient
from app.models import (
    CardJobResponse,
    JobState,
    OzonAuthLoginRequest,
    OzonAuthLoginResponse,
    OzonAuthLogoutResponse,
    OzonDraftCreateRequest,
    OzonDraftCreateResponse,
)
from app.services.job_service import JobService
from app.services.ozon_auth_service import OzonAuthService
from app.services.ozon_draft_service import OzonDraftService


def build_router(
    job_service: JobService,
    ozon_auth_service: OzonAuthService,
    ozon_base_url: str,
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

    return router
