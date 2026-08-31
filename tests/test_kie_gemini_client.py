import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from app.clients.kie_ai_client import KieAIGeminiChatClient


class KieGeminiClientTests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.requests = []
        self.http_client_class = httpx.Client
        self.sleep = self.contexts.enter_context(patch("app.clients.kie_ai_client.time.sleep"))
        self.contexts.enter_context(patch("app.clients.kie_ai_client.logger"))
        self.client = KieAIGeminiChatClient("test-key")
        self.upload = self.contexts.enter_context(patch.object(
            self.client._image_client, "_upload", return_value="https://example.com/product.png",
        ))

    def responses(self, *responses):
        outcomes = iter(responses)

        def handler(request):
            self.requests.append(request)
            return next(outcomes)

        transport = httpx.MockTransport(handler)
        self.contexts.enter_context(patch(
            "app.clients.kie_ai_client.httpx.Client",
            side_effect=lambda **kwargs: self.http_client_class(transport=transport),
        ))

    @staticmethod
    def success(text, finish_reason="stop"):
        return httpx.Response(200, json={"choices": [{
            "message": {"role": "assistant", "content": text}, "finish_reason": finish_reason,
        }]})

    def test_vision_uses_gemini_image_url_format(self):
        self.responses(self.success('{"product_name":"Мышь"}'))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "product.png"
            path.write_bytes(b"image-bytes")
            self.assertEqual(self.client.analyze_product(path, "Analyze"), {"product_name": "Мышь"})
            self.upload.assert_called_once_with(path)
        request = self.requests[0]
        self.assertIn("/gemini-2.5-flash/", str(request.url))
        body = json.loads(request.content)
        parts = body["messages"][-1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "Analyze"})
        self.assertEqual(parts[1], {"type": "image_url", "image_url": {
            "url": "https://example.com/product.png",
        }})
        self.assertFalse(body["include_thoughts"])

    def test_failed_upload_does_not_submit_analysis(self):
        self.upload.side_effect = RuntimeError("upload failed")
        with patch.object(self.client, "_request_json") as request:
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                self.client.analyze_product(Path("product.jpg"), "Analyze")
            request.assert_not_called()

    def test_chat_preserves_system_prompt_and_limit(self):
        self.responses(self.success("Ответ"))
        self.assertEqual(self.client.chat("System", "Question", max_tokens=1200), "Ответ")
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "System"}, {"role": "user", "content": "Question"},
        ])
        self.assertEqual(body["max_tokens"], 1200)

    def test_listing_recovers_from_temporary_error(self):
        self.responses(httpx.Response(503), self.success('```json\n{"title":"Мышь"}\n```'))
        self.assertEqual(self.client.generate_listing("JSON listing"), {"title": "Мышь"})
        self.sleep.assert_called_once_with(2)
        self.assertEqual(self.requests[0].content, self.requests[1].content)

    def test_http_200_application_error_is_not_a_success(self):
        self.responses(httpx.Response(200, json={"code": 401, "msg": "Unauthorized"}))
        with self.assertRaisesRegex(RuntimeError, "код 401"):
            self.client.chat("System", "Question")
        self.sleep.assert_not_called()

    def test_empty_response_is_rejected(self):
        self.responses(httpx.Response(200, json={"choices": []}))
        with self.assertRaisesRegex(RuntimeError, "не вернул текст"):
            self.client.chat("System", "Question")

    def test_truncated_listing_is_repaired_by_gemini(self):
        self.responses(self.success('{"title":', finish_reason="length"), self.success('{"title":"Мышь"}'))
        self.assertEqual(self.client.generate_listing("JSON listing"), {"title": "Мышь"})
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all("/gemini-2.5-flash/" in str(request.url) for request in self.requests))
        repair = json.loads(self.requests[1].content)
        self.assertIn("repair malformed JSON", repair["messages"][0]["content"])
        self.sleep.assert_not_called()

    def test_truncated_analysis_retries_with_larger_budget(self):
        self.responses(self.success('{"product_name":', finish_reason="length"), self.success('{"product_name":"Мышь"}'))
        result = self.client.analyze_product(Path("product.jpg"), "Analyze")
        self.assertEqual(result, {"product_name": "Мышь"})
        bodies = [json.loads(request.content) for request in self.requests]
        self.assertEqual([body["max_tokens"] for body in bodies], [4096, 8192])
        self.assertEqual(bodies[0]["messages"], bodies[1]["messages"])
        self.upload.assert_called_once()

    def test_non_object_json_is_rejected_after_bounded_repair(self):
        self.responses(self.success("[]"), self.success("[]"))
        with self.assertRaisesRegex(RuntimeError, "невалидный JSON"):
            self.client.generate_listing("JSON listing")
        self.assertEqual(len(self.requests), 2)


if __name__ == "__main__":
    unittest.main()
