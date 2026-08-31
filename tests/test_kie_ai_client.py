import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import httpx

from app.clients.kie_ai_client import KieAIChatClient


class KieAIChatClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.http_client_class = httpx.Client
        self.sleep = self.contexts.enter_context(
            patch("app.clients.kie_ai_client.time.sleep")
        )
        self.contexts.enter_context(patch("app.clients.kie_ai_client.logger"))
        self.client = KieAIChatClient("test-key")

    def mock_responses(self, *responses):
        outcomes = iter(responses)

        def handler(request):
            self.requests.append(request)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        transport = httpx.MockTransport(handler)
        self.contexts.enter_context(patch(
            "app.clients.kie_ai_client.httpx.Client",
            side_effect=lambda **kwargs: self.http_client_class(transport=transport),
        ))

    @staticmethod
    def success(text="ok"):
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

    def test_recovers_from_503_and_preserves_request(self):
        self.mock_responses(httpx.Response(503), httpx.Response(503), self.success())
        result = self.client.chat("system instruction", "user prompt", max_tokens=123)
        self.assertEqual(result, "ok")
        self.assertEqual(len(self.requests), 3)
        bodies = [json.loads(request.content) for request in self.requests]
        self.assertTrue(all(body == bodies[0] for body in bodies))
        self.assertEqual(bodies[0]["system"], "system instruction")
        self.assertEqual(bodies[0]["max_tokens"], 123)
        self.assertEqual(bodies[0]["messages"], [{"role": "user", "content": "user prompt"}])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 4])

    def test_persistent_outage_is_bounded_and_readable(self):
        self.mock_responses(*(httpx.Response(503) for _ in range(4)))
        with self.assertRaisesRegex(RuntimeError, r"Kie.ai.*HTTP 503.*4 попыток") as caught:
            self.client.chat("system", "prompt")
        self.assertIsInstance(caught.exception.__cause__, httpx.HTTPStatusError)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 4, 8])

    def test_auth_and_invalid_requests_are_not_retried(self):
        for status in (400, 401, 402, 403, 404, 422):
            with self.subTest(status=status):
                self.mock_responses(httpx.Response(status))
                with self.assertRaises(httpx.HTTPStatusError):
                    self.client.chat("system", "prompt")
        self.assertEqual(len(self.requests), 6)
        self.sleep.assert_not_called()

    def test_retry_after_seconds_is_respected(self):
        self.mock_responses(httpx.Response(429, headers={"Retry-After": "12"}), self.success())
        self.assertEqual(self.client.chat("system", "prompt"), "ok")
        self.sleep.assert_called_once_with(12)

    def test_retry_after_http_date_is_respected(self):
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.mock_responses(
            httpx.Response(503, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
            self.success(),
        )
        self.assertEqual(self.client.chat("system", "prompt"), "ok")
        delay = self.sleep.call_args.args[0]
        self.assertGreater(delay, 25)
        self.assertLessEqual(delay, 30)

    def test_long_retry_after_fails_without_retrying_early(self):
        self.mock_responses(httpx.Response(503, headers={"Retry-After": "3600"}))
        with self.assertRaisesRegex(RuntimeError, "через несколько минут"):
            self.client.chat("system", "prompt")
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_called()

    def test_invalid_retry_after_uses_backoff(self):
        self.mock_responses(httpx.Response(503, headers={"Retry-After": "invalid"}), self.success())
        self.assertEqual(self.client.chat("system", "prompt"), "ok")
        self.sleep.assert_called_once_with(2)

    def test_connection_failure_recovers(self):
        self.mock_responses(httpx.ConnectError("connection failed"), self.success())
        self.assertEqual(self.client.chat("system", "prompt"), "ok")
        self.sleep.assert_called_once_with(2)

    def test_persistent_connection_failure_is_bounded(self):
        self.mock_responses(*(httpx.ConnectTimeout("connection timed out") for _ in range(4)))
        with self.assertRaisesRegex(RuntimeError, "Не удалось подключиться"):
            self.client.chat("system", "prompt")
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_read_timeout_does_not_duplicate_request(self):
        self.mock_responses(httpx.ReadTimeout("response timed out"))
        with self.assertRaises(httpx.ReadTimeout):
            self.client.chat("system", "prompt")
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_called()

    def test_invalid_json_gets_one_repair_attempt(self):
        self.mock_responses(self.success("invalid JSON"), self.success("still invalid"))
        with self.assertRaisesRegex(RuntimeError, "невалидный JSON"):
            self.client.generate_listing("prompt")
        self.assertEqual(len(self.requests), 2)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
