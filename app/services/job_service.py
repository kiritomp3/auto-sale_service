import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from app.models import JobState
from app.repositories.job_repository import RedisJobRepository
from app.services.card_generation_service import CardGenerationService


register_heif_opener()

HEIF_IMAGE_BRANDS = {"heic", "heix", "hevc", "hevx", "mif1", "msf1"}
HEIF_IMAGE_EXTENSIONS = {".heic", ".heif"}
SUPPORTED_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
SUPPORTED_IMAGE_ERROR = "Выберите изображение в формате PNG, JPG, WEBP, GIF, HEIC или HEIF."
READ_IMAGE_ERROR = "Не удалось прочитать изображение. Загрузите другое фото в формате PNG, JPG, WEBP, GIF, HEIC или HEIF."


def _has_heif_brand(image_bytes: bytes) -> bool:
    if len(image_bytes) < 12 or image_bytes[4:8] != b"ftyp":
        return False

    brands_end = min(len(image_bytes), 64)
    for offset in range(8, brands_end - 3, 4):
        brand = image_bytes[offset:offset + 4].decode("ascii", errors="ignore").lower()
        if brand in HEIF_IMAGE_BRANDS:
            return True
    return False


def _detect_supported_extension(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes[:6] in {b"GIF87a", b"GIF89a"}:
        return ".gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return ".webp"
    if _has_heif_brand(image_bytes):
        return ".heic"
    return ""


def _verify_image_readable(image_bytes: bytes) -> None:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise RuntimeError(READ_IMAGE_ERROR) from exc


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _convert_heif_to_jpeg(image_bytes: bytes) -> bytes:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            image = _flatten_to_rgb(image)
            output = BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
            return output.getvalue()
    except (OSError, UnidentifiedImageError) as exc:
        raise RuntimeError(READ_IMAGE_ERROR) from exc


class JobService:
    def __init__(
        self,
        job_repository: RedisJobRepository,
        card_generation_service: CardGenerationService,
        executor: ThreadPoolExecutor,
        temp_dir: Path,
        output_dir: Path,
    ):
        self._job_repository = job_repository
        self._card_generation_service = card_generation_service
        self._executor = executor
        self._temp_dir = temp_dir
        self._output_dir = output_dir
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _run_job(self, job_id: str, image_path: Path, refinement_prompt: str, image_size: str | None) -> None:
        self._job_repository.save(JobState(job_id=job_id, status="processing"))
        try:
            result = self._card_generation_service.build_result(image_path, refinement_prompt, job_id, image_size)
            result_path = self._output_dir / f"{job_id}_result.json"
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self._job_repository.save(JobState(**result))
        except Exception as error:
            self._job_repository.save(JobState(job_id=job_id, status="failed", error=str(error)))

    def enqueue(
        self,
        image_bytes: bytes,
        image_filename: str,
        refinement_prompt: str,
        image_size: str | None = None,
    ) -> JobState:
        job_id = uuid.uuid4().hex
        image_path = self._prepare_input_image(image_bytes, image_filename, job_id)

        queued_job = JobState(job_id=job_id, status="queued")
        self._job_repository.save(queued_job)
        self._executor.submit(self._run_job, job_id, image_path, refinement_prompt, image_size)
        return queued_job

    def get(self, job_id: str) -> JobState | None:
        return self._job_repository.get(job_id)

    def _prepare_input_image(self, image_bytes: bytes, image_filename: str, job_id: str) -> Path:
        extension = Path(image_filename).suffix.lower()
        detected_extension = _detect_supported_extension(image_bytes)

        if not detected_extension:
            raise RuntimeError(SUPPORTED_IMAGE_ERROR)

        if extension not in SUPPORTED_IMAGE_EXTENSIONS and extension not in HEIF_IMAGE_EXTENSIONS:
            extension = detected_extension

        if extension in HEIF_IMAGE_EXTENSIONS or detected_extension in HEIF_IMAGE_EXTENSIONS:
            image_bytes = _convert_heif_to_jpeg(image_bytes)
            extension = ".jpg"
        else:
            _verify_image_readable(image_bytes)
            extension = detected_extension if detected_extension in SUPPORTED_IMAGE_EXTENSIONS else extension

        image_path = self._temp_dir / f"{job_id}{extension}"
        image_path.write_bytes(image_bytes)
        return image_path

