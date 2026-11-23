import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from configuration import ledger_config, read_config
from model_adapter import ModelError, MuseAdapter
from model_smoke_test import SCHEMA, main


class MuseTests(unittest.TestCase):
    def setUp(self):
        self.sdk_patch = patch("model_adapter.OpenAI")
        self.sdk = self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)
        self.http_patch = patch("model_adapter.httpx.Client")
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.adapter = MuseAdapter(api_key="fake_meta_secret", base_url="https://api.meta.ai/v1",
                                   model="muse-spark-1.3", forbidden_secrets=("fake_ledger_secret",))
        self.call = self.sdk.return_value.chat.completions.create
        self.reply('{"status":"ok"}')

    def reply(self, content, finish="stop", refusal=None):
        self.call.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content=content, refusal=refusal))])

    def generate(self, payload=None):
        return self.adapter.generate("Return the test status.", payload or {"task": "test"}, SCHEMA)

    def test_valid_json_and_transport_settings(self):
        self.assertEqual(self.generate(), {"status": "ok"})
        self.assertEqual(self.sdk.call_args.kwargs["max_retries"], 0)
        self.assertEqual(self.sdk.call_args.kwargs["timeout"], 30)
        self.http.assert_called_once_with(trust_env=False, follow_redirects=False)
        request = self.call.call_args.kwargs
        self.assertEqual(request["response_format"]["json_schema"]["schema"], SCHEMA)
        self.assertNotIn("fake_meta_secret", json.dumps(request))
        self.assertNotIn("fake_ledger_secret", json.dumps(request))
        self.adapter.close()
        self.sdk.return_value.close.assert_called_once()

    def test_reject_secret_values_and_nested_credential_fields_before_call(self):
        for payload in [{"text": "fake_meta_secret"}, {"text": "fake_ledger_secret"},
                        {"nested": [{"TRADER_API_KEY": "other"}]}, {"Authorization": "other"},
                        {"price": float("nan")}, {"price": float("inf")}]:
            with self.subTest(payload=list(payload)), self.assertRaises(ModelError):
                self.generate(payload)
        with self.assertRaises(ModelError):
            self.adapter.generate("fake_meta_secret", {}, SCHEMA)
        self.call.assert_not_called()

    def test_bad_responses_fail_without_echo(self):
        for content in ['not JSON fake_meta_secret', '{"status":"wrong"}',
                        '{"status":"ok","extra":1}', '{}', '[]',
                        '{"status":NaN}', '{"status":1e999}',
                        '{"status":"wrong","status":"ok"}', '', None,
                        '{"status":"fake_ledger_secret"}']:
            self.reply(content)
            with self.subTest(content=content), self.assertRaises(ModelError) as error:
                self.generate()
            self.assertNotIn("fake_meta_secret", str(error.exception))
            self.assertNotIn("fake_ledger_secret", str(error.exception))
            self.assertEqual(error.exception.category, "response")

    def test_truncation_refusal_and_missing_choices(self):
        for finish in ("length", "content_filter", "tool_calls"):
            self.reply('{"status":"ok"}', finish=finish)
            with self.assertRaises(ModelError):
                self.generate()
        self.reply('{"status":"ok"}', refusal="Refused")
        with self.assertRaises(ModelError):
            self.generate()
        self.call.return_value = SimpleNamespace(choices=[])
        with self.assertRaises(ModelError):
            self.generate()

    def test_network_failure_is_safe_and_not_retried(self):
        self.call.side_effect = APIConnectionError(message="fake_meta_secret", request=httpx.Request("POST", "https://api.meta.ai/v1"))
        with self.assertRaises(ModelError) as error:
            self.generate()
        self.assertNotIn("fake_meta_secret", str(error.exception))
        self.assertEqual(error.exception.category, "request")
        self.assertIsNone(error.exception.status)
        self.call.assert_called_once()

    def test_timeout_and_provider_status_are_distinct_and_safe(self):
        from trader_dry_run import failure_message
        request = httpx.Request("POST", "https://api.meta.ai/v1")
        errors = [
            (APITimeoutError(request=request), "timed out"),
            (APIStatusError("fake_meta_secret", response=httpx.Response(429, request=request),
                            body={"error": "fake_ledger_secret"}), "HTTP 429"),
        ]
        for exception, expected in errors:
            self.call.reset_mock()
            self.call.side_effect = exception
            with self.assertRaises(ModelError) as error:
                self.generate()
            message = failure_message(error.exception)
            self.assertIn(expected, message)
            self.assertNotIn("fake_meta_secret", message)
            self.assertNotIn("fake_ledger_secret", message)
            self.call.assert_called_once()

    def test_invalid_endpoint_and_schema(self):
        for url in ["http://api.meta.ai/v1", "https://other.test/v1", "https://api.meta.ai/v1?x=1"]:
            with self.assertRaises(ValueError):
                MuseAdapter(api_key="fake_meta_secret", base_url=url, model="muse-spark-1.3")
        for schema in [{"type": "invalid"}, {"type": "array"}]:
            with self.assertRaises(ModelError):
                self.adapter.generate("test", {}, schema)
        self.call.assert_not_called()

    def test_config_is_filtered_and_duplicate_keys_rejected(self):
        config = dict(LEDGER_BASE_URL="https://ledger.chanyeun.com/api/v1", TRADER_API_KEY="fake_ledger_secret",
                      LLM_PROVIDER="meta_muse", META_BASE_URL="https://api.meta.ai/v1",
                      MUSE_MODEL="muse-spark-1.3", META_API_KEY="fake_meta_secret")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(config))
            self.assertEqual(set(ledger_config(path)), {"LEDGER_BASE_URL", "TRADER_API_KEY"})
            adapter = MuseAdapter.from_config(path)
            with self.assertRaises(ModelError):
                adapter.generate("test", {"text": "fake_ledger_secret"}, SCHEMA)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(path)]), 0)
            self.assertIn("check OK", output.getvalue())
            self.assertNotIn("fake_meta_secret", output.getvalue())
            self.reply("bad response fake_meta_secret")
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(main(["--config", str(path)]), 1)
            self.assertNotIn("fake_meta_secret", output.getvalue())
            path.write_text('{"META_API_KEY":"a","META_API_KEY":"b"}')
            with self.assertRaises(ValueError):
                read_config(path)


if __name__ == "__main__":
    unittest.main()
