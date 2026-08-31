from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from app.clients.json_utils import parse_llm_json_object


logger = logging.getLogger(__name__)


_SIZE_TO_ASPECT: dict[str, str] = {
    "1024x1024": "1:1",
    "1024x1536": "2:3",
    "1536x1024": "3:2",
    "auto": "auto",
}


class KieAITransientError(RuntimeError):
    pass


def _is_transient_task_error(message: str | None) -> bool:
    normalized = (message or "").strip().lower()
    return any(
        token in normalized
        for token in (
            "internal error",
            "timeout",
            "temporarily",
            "temporary",
            "try again",
            "rate limit",
            "server error",
            "bad gateway",
        )
    )


class KieAIImageClient:
    """
    Image-to-image через kie.ai GPT Image 2.

    Поток: upload image → createTask → poll recordInfo → download result.
    """

    _UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
    _CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
    _STATUS_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"
    _MODEL = "gpt-image-2-image-to-image"

    def __init__(self, api_key: str, poll_interval: int = 5, timeout: int = 300, max_attempts: int = 3):
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)

    def _upload(self, image_path: Path) -> str:
        suffix = image_path.suffix.lower().lstrip(".")
        mime = f"image/{'jpeg' if suffix in ('jpg', 'jpeg') else suffix}"
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                self._UPLOAD_URL,
                headers=self._headers,
                files={"file": (image_path.name, image_path.read_bytes(), mime)},
                data={"uploadPath": "wbcards"},
            )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"kie.ai upload failed: {body.get('msg')}")
        return body["data"]["downloadUrl"]

    def _create_task(self, prompt: str, image_urls: list[str], aspect_ratio: str = "1:1") -> str:
        payload = {
            "model": self._MODEL,
            "input": {
                "prompt": prompt,
                "input_urls": image_urls,
                "aspect_ratio": aspect_ratio,
                "resolution": "1K",
            },
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                self._CREATE_URL,
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 200:
            raise RuntimeError(f"kie.ai createTask failed: {body.get('msg')}")
        return body["data"]["taskId"]

    def _poll(self, task_id: str) -> str:
        deadline = time.monotonic() + self._timeout
        with httpx.Client(timeout=15) as client:
            while time.monotonic() < deadline:
                resp = client.get(
                    self._STATUS_URL,
                    headers=self._headers,
                    params={"taskId": task_id},
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                state = data.get("state")
                if state == "success":
                    result = json.loads(data["resultJson"])
                    return result["resultUrls"][0]
                if state == "fail":
                    fail_message = data.get("failMsg")
                    if _is_transient_task_error(fail_message):
                        raise KieAITransientError(f"kie.ai task failed: {fail_message}")
                    raise RuntimeError(f"kie.ai task failed: {fail_message}")
                time.sleep(self._poll_interval)
        raise TimeoutError(f"kie.ai task {task_id} не завершился за {self._timeout}с")

    def _run_image_task_with_retries(self, prompt: str, image_urls: list[str], aspect_ratio: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            task_id = self._create_task(prompt, image_urls, aspect_ratio)
            try:
                return self._poll(task_id)
            except KieAITransientError as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                time.sleep(min(2 * attempt, 8))
        if last_error is not None:
            raise RuntimeError(
                "kie.ai task failed after retries: temporary internal image provider error"
            ) from last_error
        raise RuntimeError("kie.ai task failed before receiving a result")

    def generate_card_image_b64(self, prompt: str, image_path: Path, image_size: str | None = None) -> str:
        aspect_ratio = _SIZE_TO_ASPECT.get(image_size or "", "1:1")
        image_url = self._upload(image_path)
        result_url = self._run_image_task_with_retries(prompt, [image_url], aspect_ratio)
        return self._download_b64(result_url)

    def generate_from_images_b64(
        self,
        prompt: str,
        image_paths: list[Path],
        image_size: str | None = None,
    ) -> str:
        """Image-to-image с несколькими входными изображениями.

        Используется для try-on: [фото модели, фото одежды] → одетая модель.
        Порядок image_paths важен и интерпретируется промптом.
        """
        if not image_paths:
            raise ValueError("Нужно хотя бы одно изображение")
        aspect_ratio = _SIZE_TO_ASPECT.get(image_size or "", "2:3")
        image_urls = [self._upload(path) for path in image_paths]
        result_url = self._run_image_task_with_retries(prompt, image_urls, aspect_ratio)
        return self._download_b64(result_url)

    def _download_b64(self, result_url: str) -> str:
        with httpx.Client(timeout=60) as client:
            resp = client.get(result_url)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("utf-8")


class KieAIChatClient:
    """
    Текстовый клиент через kie.ai Claude Haiku 4.5.

    Совместим с Anthropic Messages API.
    """

    _URL = "https://api.kie.ai/claude/v1/messages"
    _MODEL = "claude-haiku-4-5"
    _SERVICE_LABEL = "Claude"
    _MAX_ATTEMPTS = 4
    _RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 529}
    _MAX_RETRY_DELAY = 60.0

    _DEFAULT_MAX_TOKENS = 4096
    _LISTING_MAX_TOKENS = 8192
    _REPAIR_MAX_TOKENS = 8192
    _JSON_SYSTEM = "Return only a valid JSON object. Do not use markdown or explanatory text."
    _JSON_REPAIR_SYSTEM = (
        "You repair malformed JSON. Return only one valid JSON object. "
        "Preserve fields and values from the source as much as possible. Do not use markdown."
    )

    _MIME = {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }

    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @dataclass(frozen=True)
    class _TextResponse:
        text: str
        stop_reason: str | None

    def _post_response(
        self,
        messages: list,
        system: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> _TextResponse:
        body: dict = {"model": self._MODEL, "max_tokens": max_tokens, "stream": False, "messages": messages}
        if system:
            body["system"] = system
        payload = self._request_json(body)
        content = payload.get("content", [])
        if isinstance(content, str):
            text = content
        else:
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if not text:
            raise RuntimeError("AI вернул пустой текстовый ответ.")
        return self._TextResponse(text=text, stop_reason=payload.get("stop_reason"))

    def _post(
        self,
        messages: list,
        system: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        return self._post_response(messages, system=system, max_tokens=max_tokens).text

    def _request_json(self, body: dict) -> dict:
        with httpx.Client(timeout=180) as client:
            for attempt in range(1, self._MAX_ATTEMPTS + 1):
                delay = float(2 ** attempt)
                try:
                    resp = client.post(self._URL, headers=self._headers, json=body)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status not in self._RETRYABLE_STATUSES:
                        raise
                    retry_after = self._retry_after_seconds(exc.response)
                    # Do not retry before the provider's requested time, or
                    # occupy a job worker with an unbounded wait.
                    if attempt == self._MAX_ATTEMPTS or (
                        retry_after is not None and retry_after > self._MAX_RETRY_DELAY
                    ):
                        raise RuntimeError(
                            f"Сервис Kie.ai ({self._SERVICE_LABEL}) временно недоступен (HTTP {status}). "
                            f"Не удалось выполнить запрос после {attempt} попыток. "
                            "Попробуйте запустить задачу через несколько минут."
                        ) from exc
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                    reason = f"HTTP {status}"
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    # Only connection failures are safe to retry here; after a
                    # read/write timeout the paid request may already be running.
                    if attempt == self._MAX_ATTEMPTS:
                        raise RuntimeError(
                            f"Не удалось подключиться к Kie.ai ({self._SERVICE_LABEL}) после нескольких попыток. "
                            "Попробуйте запустить задачу через несколько минут."
                        ) from exc
                    reason = type(exc).__name__
                else:
                    return resp.json()
                logger.warning(
                    "Kie.ai %s request failed (%s), attempt %d/%d; retrying in %.1fs",
                    self._SERVICE_LABEL, reason, attempt, self._MAX_ATTEMPTS, delay,
                )
                time.sleep(delay)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After", "").strip()
        if not value:
            return None
        if value.isascii() and value.isdigit():
            return float(value)
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _parse_json(text: str) -> dict:
        return parse_llm_json_object(text)

    def _repair_json(self, raw_text: str, parse_error: Exception) -> dict:
        repair_prompt = (
            "The previous model response was supposed to be JSON, but parsing failed.\n"
            f"Parser error: {parse_error}\n\n"
            "Return only corrected valid JSON object based on this source:\n"
            f"{raw_text}"
        )
        response = self._post_response(
            [{"role": "user", "content": repair_prompt}],
            system=self._JSON_REPAIR_SYSTEM,
            max_tokens=self._REPAIR_MAX_TOKENS,
        )
        return self._parse_json(response.text)

    def _post_json(
        self,
        messages: list,
        *,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        retry_max_tokens: int | None = None,
    ) -> dict:
        response = self._post_response(messages, system=self._JSON_SYSTEM, max_tokens=max_tokens)
        try:
            return self._parse_json(response.text)
        except (json.JSONDecodeError, ValueError) as first_error:
            if response.stop_reason == "max_tokens" and retry_max_tokens and retry_max_tokens > max_tokens:
                response = self._post_response(messages, system=self._JSON_SYSTEM, max_tokens=retry_max_tokens)
                try:
                    return self._parse_json(response.text)
                except (json.JSONDecodeError, ValueError) as retry_error:
                    first_error = retry_error

            try:
                return self._repair_json(response.text, first_error)
            except (json.JSONDecodeError, ValueError) as repair_error:
                raise RuntimeError(
                    "AI вернул невалидный JSON с описанием карточки. "
                    "Повторите генерацию или выберите один маркетплейс вместо всех."
                ) from repair_error

    def analyze_product(self, image_path: Path, prompt: str) -> dict:
        suffix = image_path.suffix.lower()
        mime = self._MIME.get(suffix, "image/jpeg")
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                ],
            }
        ]
        return self._post_json(messages, max_tokens=self._DEFAULT_MAX_TOKENS, retry_max_tokens=self._LISTING_MAX_TOKENS)

    def generate_listing(self, prompt: str) -> dict:
        messages = [{"role": "user", "content": prompt}]
        return self._post_json(messages, max_tokens=self._LISTING_MAX_TOKENS)

    def chat(self, system: str, user_prompt: str, max_tokens: int = 1200) -> str:
        messages = [{"role": "user", "content": user_prompt}]
        return self._post(messages, system=system, max_tokens=max_tokens)


class KieAIGeminiChatClient(KieAIChatClient):
    """Gemini text and vision through Kie.ai's Chat Completions endpoint.

    Reuses retries and listing/chat methods; overrides the Claude wire format.
    """

    _URL = "https://api.kie.ai/gemini-2.5-flash/v1/chat/completions"
    _MODEL = "gemini-2.5-flash"
    _SERVICE_LABEL = "Gemini"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self._image_client = KieAIImageClient(api_key)

    def _post_response(
        self, messages: list, system: str | None = None, max_tokens: int = 4096,
    ) -> KieAIChatClient._TextResponse:
        if system:
            messages = [{"role": "system", "content": system}, *messages]
        body = {
            "messages": messages,
            "stream": False,
            "include_thoughts": False,
            "max_tokens": max_tokens,
        }
        result = self._request_json(body)
        # Kie.ai can return application errors with HTTP 200.
        if result.get("error") or result.get("code", 200) != 200:
            code = result.get("code", "api_error")
            raise RuntimeError(f"Kie.ai (Gemini) отклонил запрос (код {code}).")
        choices = result.get("choices") or []
        if not choices:
            raise RuntimeError("Kie.ai (Gemini) не вернул текст ответа.")
        choice = choices[0]
        text = (choice.get("message") or {}).get("content")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Kie.ai (Gemini) не вернул текст ответа.")
        stop_reason = choice.get("finish_reason")
        if stop_reason == "length":
            stop_reason = "max_tokens"
        return self._TextResponse(text=text, stop_reason=stop_reason)

    def analyze_product(self, image_path: Path, prompt: str) -> dict:
        # The Gemini gateway rejects large inline images. Upload the original
        # file instead so normal phone photos do not hit the data-URL limit.
        image_url = self._image_client._upload(image_path)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }]
        return self._post_json(
            messages, max_tokens=self._DEFAULT_MAX_TOKENS, retry_max_tokens=self._LISTING_MAX_TOKENS,
        )
