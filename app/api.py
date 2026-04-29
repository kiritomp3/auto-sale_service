from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.models import CardJobResponse, JobState
from app.services.job_service import JobService


def build_router(job_service: JobService) -> APIRouter:
    router = APIRouter()

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

    return router

