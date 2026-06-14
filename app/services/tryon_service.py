from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.clients.kie_ai_client import KieAIImageClient
from app.models import JobState, TryOnModel
from app.repositories.job_repository import RedisJobRepository
from app.services.job_service import prepare_image_file


TRYON_PROMPT = """
На первом изображении — фотомодель в определённой позе. На втором изображении — предмет одежды.

ЗАДАЧА: надень одежду со второго изображения на модель с первого изображения.

ЖЁСТКИЕ ПРАВИЛА:
1) Сохрани модель с первого фото без изменений: поза, телосложение, лицо, причёска, кожа, фон и освещение остаются прежними.
2) Замени только одежду модели на предмет со второго фото.
3) Точно перенеси одежду: цвет, крой, материал, принты, фурнитуру и пропорции — как на втором фото.
4) Одежда должна естественно сидеть по фигуре и позе модели, с реалистичными складками и тенями.
5) Не добавляй посторонних предметов, логотипов и текста.

Результат — фотореалистичное изображение модели в этой одежде, пригодное для карточки товара.
""".strip()


class TryOnService:
    """Наложение одежды на фотомодель через kie.ai (image-to-image, 2 входа)."""

    def __init__(
        self,
        job_repository: RedisJobRepository,
        image_client: KieAIImageClient,
        executor: ThreadPoolExecutor,
        temp_dir: Path,
        output_dir: Path,
        models_dir: Path,
    ):
        self._job_repository = job_repository
        self._image_client = image_client
        self._executor = executor
        self._temp_dir = temp_dir
        self._output_dir = output_dir
        self._models_dir = models_dir
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._models_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Каталог поз/моделей
    # ------------------------------------------------------------------
    def list_models(self, base_url: str = "") -> list[TryOnModel]:
        """Читает models_dir/catalog.json — список пресетов поз модели."""
        catalog_path = self._models_dir / "catalog.json"
        if not catalog_path.exists():
            return []
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        models: list[TryOnModel] = []
        for entry in raw:
            preview = entry.get("preview_url") or f"{base_url.rstrip('/')}/models/{entry['file']}"
            models.append(
                TryOnModel(
                    id=entry["id"],
                    name=entry.get("name", entry["id"]),
                    gender=entry.get("gender", ""),
                    pose=entry.get("pose", ""),
                    preview_url=preview,
                )
            )
        return models

    def _resolve_model_file(self, model_id: str) -> Path:
        catalog_path = self._models_dir / "catalog.json"
        if not catalog_path.exists():
            raise ValueError("Каталог моделей не настроен")
        for entry in json.loads(catalog_path.read_text(encoding="utf-8")):
            if entry["id"] == model_id:
                path = self._models_dir / entry["file"]
                if not path.exists():
                    raise ValueError(f"Файл модели {model_id} не найден")
                return path
        raise ValueError(f"Модель {model_id} не найдена")

    # ------------------------------------------------------------------
    # Постановка задачи
    # ------------------------------------------------------------------
    def enqueue(
        self,
        garment_bytes: bytes,
        garment_filename: str,
        model_id: str | None = None,
        model_bytes: bytes | None = None,
        model_filename: str | None = None,
        extra_prompt: str = "",
    ) -> JobState:
        job_id = uuid.uuid4().hex

        garment_path = prepare_image_file(garment_bytes, garment_filename, self._temp_dir, f"{job_id}_garment")

        if model_bytes and model_filename:
            model_path = prepare_image_file(model_bytes, model_filename, self._temp_dir, f"{job_id}_model")
        elif model_id:
            model_path = self._resolve_model_file(model_id)
        else:
            raise ValueError("Нужно указать model_id или загрузить model_image")

        queued = JobState(job_id=job_id, status="queued", kind="tryon")
        self._job_repository.save(queued)
        self._executor.submit(self._run, job_id, model_path, garment_path, extra_prompt)
        return queued

    def get(self, job_id: str) -> JobState | None:
        return self._job_repository.get(job_id)

    def _run(self, job_id: str, model_path: Path, garment_path: Path, extra_prompt: str) -> None:
        self._job_repository.save(JobState(job_id=job_id, status="processing", kind="tryon"))
        try:
            prompt = TRYON_PROMPT
            if extra_prompt.strip():
                prompt = f"{prompt}\n\nДополнительно от пользователя: {extra_prompt.strip()}"

            # Порядок важен: [модель, одежда]
            result_b64 = self._image_client.generate_from_images_b64(
                prompt, [model_path, garment_path], image_size="1024x1536"
            )
            output_path = self._output_dir / f"{job_id}_tryon.png"
            output_path.write_bytes(__import__("base64").b64decode(result_b64))

            self._job_repository.save(
                JobState(
                    job_id=job_id,
                    status="done",
                    kind="tryon",
                    images=[str(output_path)],
                    input={
                        "model_path": str(model_path),
                        "garment_path": str(garment_path),
                        "extra_prompt": extra_prompt,
                    },
                )
            )
        except Exception as error:
            self._job_repository.save(
                JobState(job_id=job_id, status="failed", kind="tryon", error=str(error))
            )
