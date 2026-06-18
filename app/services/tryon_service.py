from __future__ import annotations

import base64
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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

DEFAULT_TRYON_IMAGE_COUNT = 1
MIN_TRYON_IMAGE_COUNT = 1
MAX_TRYON_IMAGE_COUNT = 6


def normalize_tryon_image_count(value: int | str | None) -> int:
    try:
        parsed = int(value) if value is not None else DEFAULT_TRYON_IMAGE_COUNT
    except (TypeError, ValueError) as exc:
        raise RuntimeError("n_cards must be an integer") from exc
    if parsed < MIN_TRYON_IMAGE_COUNT or parsed > MAX_TRYON_IMAGE_COUNT:
        raise RuntimeError(f"n_cards must be between {MIN_TRYON_IMAGE_COUNT} and {MAX_TRYON_IMAGE_COUNT}")
    return parsed


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
        n_images: int | str | None = DEFAULT_TRYON_IMAGE_COUNT,
    ) -> JobState:
        job_id = uuid.uuid4().hex
        normalized_n_images = normalize_tryon_image_count(n_images)

        garment_path = prepare_image_file(garment_bytes, garment_filename, self._temp_dir, f"{job_id}_garment")

        if model_bytes and model_filename:
            model_path = prepare_image_file(model_bytes, model_filename, self._temp_dir, f"{job_id}_model")
        elif model_id:
            model_path = self._resolve_model_file(model_id)
        else:
            raise ValueError("Нужно указать model_id или загрузить model_image")

        queued = JobState(job_id=job_id, status="queued", kind="tryon")
        self._job_repository.save(queued)
        self._executor.submit(self._run, job_id, model_path, garment_path, extra_prompt, normalized_n_images)
        return queued

    def get(self, job_id: str) -> JobState | None:
        return self._job_repository.get(job_id)

    def _generate_single_tryon(
        self,
        prompt: str,
        model_path: Path,
        garment_path: Path,
        job_id: str,
        image_index: int,
    ) -> tuple[int, str]:
        result_b64 = self._image_client.generate_from_images_b64(
            prompt, [model_path, garment_path], image_size="1024x1536"
        )
        suffix = "" if image_index == 0 else f"_{image_index + 1}"
        output_path = self._output_dir / f"{job_id}_tryon{suffix}.png"
        output_path.write_bytes(base64.b64decode(result_b64))
        return image_index, str(output_path)

    def _generate_tryon_images(
        self,
        prompt: str,
        model_path: Path,
        garment_path: Path,
        job_id: str,
        n_images: int,
    ) -> list[str]:
        result_paths: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=n_images) as executor:
            futures = {
                executor.submit(self._generate_single_tryon, prompt, model_path, garment_path, job_id, index): index
                for index in range(n_images)
            }
            for future in as_completed(futures):
                index, path = future.result()
                result_paths[index] = path
        return [result_paths[index] for index in range(n_images)]

    def _run(self, job_id: str, model_path: Path, garment_path: Path, extra_prompt: str, n_images: int) -> None:
        self._job_repository.save(JobState(job_id=job_id, status="processing", kind="tryon"))
        try:
            prompt = TRYON_PROMPT
            if extra_prompt.strip():
                prompt = f"{prompt}\n\nДополнительно от пользователя: {extra_prompt.strip()}"

            # Порядок важен: [модель, одежда]
            images = self._generate_tryon_images(prompt, model_path, garment_path, job_id, n_images)

            self._job_repository.save(
                JobState(
                    job_id=job_id,
                    status="done",
                    kind="tryon",
                    images=images,
                    input={
                        "model_path": str(model_path),
                        "garment_path": str(garment_path),
                        "extra_prompt": extra_prompt,
                        "n_cards": n_images,
                    },
                )
            )
        except Exception as error:
            self._job_repository.save(
                JobState(job_id=job_id, status="failed", kind="tryon", error=str(error))
            )
