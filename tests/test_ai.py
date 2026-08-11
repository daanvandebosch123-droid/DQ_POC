from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from dqtool.services import ai as ai_module
from dqtool.services.ai import DRAFT_TIMEOUT_SECONDS, DEFAULT_ENDPOINT, DEFAULT_MODEL, EMAIL_PATTERN, OllamaService


class OllamaRecommendationTests(unittest.TestCase):
    def test_default_model_is_qwen3_8b(self) -> None:
        self.assertEqual("qwen3:8b", DEFAULT_MODEL)

    def test_default_endpoint_is_shared_cloudflare_endpoint(self) -> None:
        self.assertEqual("http://localhost:11434", DEFAULT_ENDPOINT)

    def test_headers_include_cloudflare_access_service_token_when_saved(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        with patch.object(ai_module, "get_ollama_access_credentials", return_value=("client-id", "client-secret")):
            headers = service._headers()

        self.assertEqual("client-id", headers["CF-Access-Client-Id"])
        self.assertEqual("client-secret", headers["CF-Access-Client-Secret"])

    def test_headers_omit_cloudflare_access_when_no_token_saved(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        with patch.object(ai_module, "get_ollama_access_credentials", return_value=None):
            headers = service._headers()

        self.assertNotIn("CF-Access-Client-Id", headers)
        self.assertNotIn("CF-Access-Client-Secret", headers)

    def test_structured_chat_disables_model_thinking(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"message": {"content": "[]"}}'
        with (
            patch.object(ai_module, "get_ollama_access_credentials", return_value=None),
            patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            service.chat("prioritize", json_mode=True, json_schema={"type": "array"})

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIs(False, payload["think"])
        self.assertEqual({"type": "array"}, payload["format"])

    def test_explanation_uses_metadata_without_finding_messages(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(return_value="The email field needs attention.")
        profile = {
            "row_count": 1,
            "columns": {"email": {"type": "VARCHAR", "sample_values": ["private@example.com"]}},
        }

        service.explain_anomalies(
            "customers", profile, [{"severity": "high", "column": "email", "message": "private@example.com"}]
        )

        self.assertNotIn("private@example.com", service.chat.call_args.args[0])

    def test_recommendations_use_metadata_without_example_values(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(return_value="## Recommended rules\n- Not Null")
        profile = {
            "row_count": 2,
            "columns": {
                "email": {
                    "type": "VARCHAR",
                    "inferred_type": "email",
                    "null_rate": 0.5,
                    "distinct_count": 1,
                    "sample_values": ["private@example.com"],
                }
            },
            "gdpr_findings": [
                {"severity": "medium", "column": "email", "category": "Personal data: email address"}
            ],
        }
        findings = [{"severity": "medium", "column": "email", "message": "e.g. private@example.com"}]
        ideas = [{"name": "email must not be null", "column": "email", "rule_type": "not_null", "config": {}}]

        result = service.recommend_actions("customers", profile, findings, ideas)

        self.assertEqual("## Recommended rules\n- Not Null", result)
        prompt = service.chat.call_args.args[0]
        self.assertNotIn("private@example.com", prompt)
        context = json.loads(prompt)
        self.assertEqual("email", context["existing_rule_ideas"][0]["field"])
        self.assertEqual({"severity": "medium", "column": "email"}, context["findings"][0])

    def test_explain_rule_failure_excludes_connection_ids(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(return_value="The source connection is unreachable.")

        result = service.explain_rule_failure(
            "orders not null",
            "not_null",
            {"column": "order_id", "source_connection_id": 7, "source_sql": "SELECT * FROM private_orders"},
            {
                "source_label": "orders.csv",
                "checked_count": 0,
                "failed_count": 0,
                "max_age_days": 3,
                "error": "connection refused",
            },
            "error",
        )

        self.assertEqual("The source connection is unreachable.", result)
        prompt = json.loads(service.chat.call_args.args[0])
        self.assertNotIn("source_connection_id", prompt["config"])
        self.assertNotIn("source_sql", prompt["config"])
        self.assertEqual("order_id", prompt["config"]["column"])
        self.assertEqual("connection refused", prompt["run_summary"]["error"])
        self.assertEqual("orders.csv", prompt["run_summary"]["source_label"])
        self.assertEqual(3, prompt["run_summary"]["max_age_days"])

    def test_draft_rule_parses_fenced_json_with_think_block_and_chooses_type(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(
            return_value=(
                "<think>the user wants an email pattern</think>\n"
                "```json\n"
                '{"rule_type": "regex", "name": "Email shape check", '
                '"config": {"column": "email", "pattern": "^[^@]+@[^@]+$"}}\n'
                "```"
            )
        )

        draft = service.draft_rule("email must look like an email address", ["email", "name"])

        self.assertEqual("regex", draft["rule_type"])
        self.assertEqual("Email shape check", draft["name"])
        self.assertEqual({"column": "email", "pattern": EMAIL_PATTERN}, draft["config"])
        prompt = json.loads(service.chat.call_args.args[0])
        self.assertEqual(["email", "name"], prompt["available_fields"])
        rule_type_ids = [entry["rule_type"] for entry in prompt["rule_type_catalog"]]
        self.assertIn("regex", rule_type_ids)
        self.assertIn("not_null", rule_type_ids)
        self.assertTrue(service.chat.call_args.kwargs["json_mode"])
        self.assertEqual(DRAFT_TIMEOUT_SECONDS, service.chat.call_args.kwargs["timeout"])
        schema = service.chat.call_args.kwargs["json_schema"]
        self.assertEqual(["rule_type", "name", "config"], schema["required"])
        self.assertEqual(["regex"], schema["properties"]["rule_type"]["enum"])
        self.assertEqual("regex", prompt["required_rule_type"])

    def test_draft_rule_raises_on_non_json_response(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(return_value="I cannot help with that.")

        with self.assertRaises(RuntimeError):
            service.draft_rule("customer id must be present", [])

    def test_email_draft_uses_real_email_field_and_email_pattern(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(
            return_value=(
                '{"rule_type": "regex", "name": "Email check", '
                '"config": {"column": "Subscription Date", "pattern": "\\\\d{4}-\\\\d{2}-\\\\d{2}"}}'
            )
        )

        draft = service.draft_rule(
            "check whether email addresses are correct",
            ["Customer Id", "Email Address", "Subscription Date"],
        )

        self.assertEqual("Email Address", draft["config"]["column"])
        self.assertEqual(EMAIL_PATTERN, draft["config"]["pattern"])

    def test_draft_rule_turns_an_after_date_requirement_into_a_date_boundary(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(
            return_value=(
                '{"rule_type": "date_validity", "name": "Subscription date", '
                '"config": {"column": "Subscription Date"}}'
            )
        )

        draft = service.draft_rule(
            "customers can only subscribe after 01/01/2022",
            ["Customer ID", "Subscription Date"],
        )

        self.assertEqual("date_validity", draft["rule_type"])
        self.assertEqual("Subscription Date", draft["config"]["column"])
        self.assertEqual("2022-01-02", draft["config"]["min_date"])
        schema = service.chat.call_args.kwargs["json_schema"]
        self.assertEqual(["date_validity"], schema["properties"]["rule_type"]["enum"])

    def test_draft_rule_interprets_after_date_is_invalid_as_a_latest_date(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(
            return_value=(
                '{"rule_type": "date_validity", "name": "Subscription cutoff", "config": '
                '{"column": "Subscription Date", "min_date": "2022-01-02", "max_date": "2021-12-31"}}'
            )
        )

        draft = service.draft_rule(
            "Customers subscribed after 01/01/2022 are invalid",
            ["Customer ID", "Subscription Date"],
        )

        self.assertEqual("2022-01-01", draft["config"]["max_date"])
        self.assertNotIn("min_date", draft["config"])

    def test_draft_rule_rejects_unavailable_field(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(
            return_value='{"rule_type": "not_null", "name": "Required", "config": {"column": "invented"}}'
        )

        with self.assertRaisesRegex(RuntimeError, "unavailable field"):
            service.draft_rule("customer id must be present", ["Customer Id"])

    def test_draft_rule_omits_sample_values_by_default(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(return_value='{"rule_type": "not_null", "name": "x", "config": {"column": "FLD_23"}}')

        service.draft_rule("FLD_23 must always be present", ["FLD_23"])

        prompt = json.loads(service.chat.call_args.args[0])
        self.assertNotIn("sample_values_by_field", prompt)

    def test_draft_rule_includes_sample_values_when_provided(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(
            return_value='{"rule_type": "allowed_values", "name": "x", "config": {"column": "FLD_23", "values": ["A", "B"]}}'
        )

        service.draft_rule(
            "FLD_23 must be one of its current values", ["FLD_23"], {"FLD_23": ["A", "B", "A", "B"]}
        )

        prompt = json.loads(service.chat.call_args.args[0])
        self.assertEqual({"FLD_23": ["A", "B", "A", "B"]}, prompt["sample_values_by_field"])

    def test_draft_rule_raises_on_unknown_rule_type(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(return_value='{"rule_type": "made_up_type", "name": "x", "config": {}}')

        with self.assertRaises(RuntimeError):
            service.draft_rule("customer id must be present", ["customer_id"])

    def test_prioritize_suggestions_filters_invalid_entries(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(
            return_value=json.dumps(
                [
                    {"index": 0, "priority": "high", "why": "Frequently null in recent runs."},
                    {"index": 1, "priority": "unknown", "why": "Not clear."},
                    {"index": 5, "priority": "Low", "why": "Out of range index."},
                    "not-a-dict",
                ]
            )
        )
        suggestions = [
            {"name": "email not null", "column": "email", "rule_type": "not_null", "reason": "always populated"},
            {"name": "status allowed values", "column": "status", "rule_type": "allowed_values", "reason": "few distinct values"},
        ]

        notes = service.prioritize_suggestions("customers", {"row_count": 10}, suggestions)

        self.assertEqual({0: {"priority": "High", "why": "Frequently null in recent runs."}}, notes)
        self.assertTrue(service.chat.call_args.kwargs["json_mode"])
        schema = service.chat.call_args.kwargs["json_schema"]
        self.assertEqual("array", schema["type"])
        self.assertEqual(2, schema["minItems"])
        self.assertEqual(["index", "priority", "why"], schema["items"]["required"])


if __name__ == "__main__":
    unittest.main()
