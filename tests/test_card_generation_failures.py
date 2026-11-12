import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import httpx

from app.models import JobState
from app.clients.kie_ai_client import KieAIImageClient, KieAIResponseError, generation_error_message
from app.services.card_generation_service import CardGenerationService
from app.services.job_service import JobService


class CardGenerationFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.text_client = Mock()
        self.image_client = Mock()
        self.service = CardGenerationService(self.text_client, self.image_client, self.root)
        self.prompt = patch.object(self.service, "_build_card_prompt", return_value="card prompt")
        self.prompt.start()
        self.addCleanup(self.prompt.stop)

    def test_failed_image_never_becomes_a_fallback_card(self):
        self.image_client.generate_card_image_b64.side_effect = RuntimeError("provider unavailable")
        result = self.service._generate_single_card({}, self.root / "source.jpg", "", "job", 0, "1024x1536", "wb")
        self.assertEqual(result, (0, "", "Сервис генерации временно недоступен. Повторите позже."))
        self.assertFalse((self.root / "job_card_1.png").exists())

    def test_all_failed_images_fail_job_instead_of_reporting_done(self):
        self.image_client.generate_card_image_b64.side_effect = RuntimeError("provider unavailable")
        repository = Mock()
        job_service = JobService(repository, self.service, Mock(), self.root / "tmp", self.root)
        job_service._run_job("job", self.root / "source.jpg", "", "1024x1536", "wb", 2)
        saved = repository.save.call_args.args[0]
        self.assertEqual(saved.status, "failed")
        self.assertIn("Не удалось сгенерировать изображения карточки", saved.error)
        self.assertNotIn("provider unavailable", saved.error)
        self.text_client.generate_listing.assert_not_called()
        self.assertFalse((self.root / "job_result.json").exists())

    def test_partial_generation_returns_only_real_cards_in_requested_order(self):
        def card(*args):
            index = args[4]
            return (index, "", "provider error") if index == 1 else (index, f"card-{index}.png", None)

        with patch.object(self.service, "_generate_single_card", side_effect=card):
            cards, errors = self.service._generate_cards({}, self.root / "source.jpg", "", "job", "1024x1536", "wb", 3)
        self.assertEqual(cards, ["card-0.png", "card-2.png"])
        self.assertEqual(errors, ["provider error"])

    def test_partial_errors_survive_job_storage_round_trip(self):
        job = JobState(job_id="job", status="done", cards=["real.png"], image_generation_errors=["provider error"])
        restored = JobState.model_validate_json(job.model_dump_json())
        self.assertEqual(restored.image_generation_errors, ["provider error"])

    def test_success_preserves_generated_output(self):
        self.image_client.generate_card_image_b64.return_value = base64.b64encode(b"generated").decode()
        index, path, error = self.service._generate_single_card({}, self.root / "source.jpg", "", "job", 0, "1024x1536", "wb")
        self.assertEqual(index, 0)
        self.assertIsNone(error)
        self.assertEqual(Path(path).read_bytes(), b"generated")

    def test_provider_response_and_signed_urls_are_never_saved_to_public_job_errors(self):
        private = "https://provider.invalid/result.png?token=private-secret"
        request = httpx.Request("GET", private)
        error = httpx.HTTPStatusError("Failed to download " + private,
            request=request, response=httpx.Response(402, request=request))
        self.image_client.generate_card_image_b64.side_effect = error
        repository = Mock()
        job_service = JobService(repository, self.service, Mock(), self.root / "tmp", self.root)
        with self.assertLogs("app.services.card_generation_service", level="WARNING") as captured:
            job_service._run_job("job", self.root / "source.jpg", "", "1024x1536", "wb", 1)
        saved = repository.save.call_args.args[0]
        self.assertIn("балансе", saved.error)
        self.assertNotIn(private, saved.model_dump_json())
        self.assertNotIn("private-secret", " ".join(captured.output))
        self.assertIn("provider_code=402", " ".join(captured.output))

    def test_image_task_rejection_preserves_balance_code_without_provider_text(self):
        original_client = httpx.Client
        transport = httpx.MockTransport(lambda request: httpx.Response(200,
            json={"code": 402, "msg": "private signed-url-and-api-key"}))
        with patch("app.clients.kie_ai_client.httpx.Client",
                   side_effect=lambda **kwargs: original_client(transport=transport)):
            with self.assertRaises(KieAIResponseError) as caught:
                KieAIImageClient("test-key")._create_task("prompt", ["https://example.test/input.jpg"])
        self.assertEqual(caught.exception.provider_code, 402)
        self.assertEqual(caught.exception.failure_kind, "image_task_rejected")
        self.assertIn("балансе", generation_error_message(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_failed_image_task_never_echoes_provider_failure_body(self):
        original_client = httpx.Client
        transport = httpx.MockTransport(lambda request: httpx.Response(200,
            json={"code": 200, "data": {"state": "fail", "failCode": 422,
                  "failMsg": "private signed-url-and-api-key"}}))
        with patch("app.clients.kie_ai_client.httpx.Client",
                   side_effect=lambda **kwargs: original_client(transport=transport)):
            with self.assertRaises(KieAIResponseError) as caught:
                KieAIImageClient("test-key")._poll("test-task")
        self.assertEqual(caught.exception.provider_code, 422)
        self.assertEqual(caught.exception.failure_kind, "image_task_failed")
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
