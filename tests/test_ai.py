from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from dqtool.services.ai import DEFAULT_MODEL, OllamaService


class OllamaRecommendationTests(unittest.TestCase):
    def test_default_model_is_qwen3_8b(self) -> None:
        self.assertEqual("qwen3:8b", DEFAULT_MODEL)

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
            {"column": "order_id", "source_connection_id": 7},
            {"checked_count": 0, "failed_count": 0, "error": "connection refused"},
            "error",
        )

        self.assertEqual("The source connection is unreachable.", result)
        prompt = json.loads(service.chat.call_args.args[0])
        self.assertNotIn("source_connection_id", prompt["config"])
        self.assertEqual("order_id", prompt["config"]["column"])
        self.assertEqual("connection refused", prompt["error"])

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
        self.assertEqual({"column": "email", "pattern": "^[^@]+@[^@]+$"}, draft["config"])
        prompt = json.loads(service.chat.call_args.args[0])
        self.assertEqual(["email", "name"], prompt["available_fields"])
        rule_type_ids = [entry["rule_type"] for entry in prompt["rule_type_catalog"]]
        self.assertIn("regex", rule_type_ids)
        self.assertIn("not_null", rule_type_ids)

    def test_draft_rule_raises_on_non_json_response(self) -> None:
        service = OllamaService(endpoint="http://ollama.test", model="test-model")
        service.chat = Mock(return_value="I cannot help with that.")

        with self.assertRaises(RuntimeError):
            service.draft_rule("customer id must be present", [])

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


if __name__ == "__main__":
    unittest.main()
