from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from dqtool.models.entities import RuleType
from dqtool.services.project import get_ollama_access_credentials, load_settings
from dqtool.services.rules import RULE_CONFIG_EXAMPLES, RULE_TEMPLATES

DEFAULT_ENDPOINT = "https://ollama.dqpocai.org"
DEFAULT_MODEL = "qwen3:8b"

EXPLANATION_SYSTEM_PROMPT = (
    "You are a data quality analyst. You receive column statistics and drift findings "
    "for a dataset. Explain what stands out and the most likely causes in plain language. "
    "Be concise (at most 150 words), factual, and do not invent columns or numbers that "
    "are not in the input. If there are no findings, say the data looks stable."
)

RECOMMENDATION_SYSTEM_PROMPT = (
    "You are a careful data quality analyst. Based only on the supplied dataset statistics, "
    "finding metadata, privacy-review categories, and existing rule-idea metadata, recommend "
    "the most valuable data-quality rules and follow-up actions. Return concise Markdown (at most "
    "300 words) with these headings: 'Recommended rules', 'Next steps', and 'Cautions'. "
    "For each recommended rule, name only a rule type and an existing field, state its priority "
    "(high, medium, or low), and explain why. Do not invent fields, values, business thresholds, "
    "SQL, or facts. Treat all suggestions as advisory and call out when a business owner must set "
    "the allowed values or threshold. Do not make legal/GDPR conclusions."
)

FAILURE_SYSTEM_PROMPT = (
    "You are a data quality analyst. You receive metadata about one data-quality rule run: its type, "
    "its configuration (field names, patterns, thresholds - never source data), pass/fail counts, and "
    "any execution error. Explain in plain language why this run most likely failed or errored, and "
    "suggest the single most useful next diagnostic step. Be concise (at most 120 words) and factual; "
    "do not invent data values, causes, or business context that are not implied by the input."
)

RULE_DRAFT_SYSTEM_PROMPT = (
    "You are a data quality analyst assistant. A user describes, in plain English, a data-quality check "
    "they want on a chosen data source. You are given the exact field names available on that source and "
    "a catalog of supported rule types, each with an id, a short description, and an example JSON settings "
    "shape. Choose the single rule type from the catalog that best matches the description, then draft its "
    "settings. Return ONLY a single JSON object (no markdown, no commentary, no <think> blocks) with three "
    "keys: \"rule_type\" (the exact id of the chosen type from the catalog), \"name\" (a short rule name, "
    "at most 8 words), and \"config\" (an object using only the keys shown in that type's example shape). "
    "Use a field name only from the provided field list; never invent a field name that is not in that "
    "list. Do not invent connection details, table names, or numeric thresholds the user did not imply. "
    "If nothing matches well, choose the closest reasonable rule type. When sample values are provided for "
    "a field, you may use them to infer its shape or meaning - for example to write a matching regex, spot "
    "a plausible numeric range, or list a field's current set of values for an allowed-values check - but "
    "only draw config values from what is actually shown to you, never invent beyond it."
)

PRIORITIZE_SYSTEM_PROMPT = (
    "You are a careful data quality analyst. You receive a list of heuristically generated rule ideas "
    "(index, rule type, field, heuristic reason) for one data source, plus its row count. For each idea, "
    "return its priority (\"High\", \"Medium\", or \"Low\") for a data quality team to implement next, and "
    "a short one-sentence reason (at most 25 words) grounded only in the given metadata. Return ONLY a "
    "JSON array of objects with keys \"index\", \"priority\", and \"why\" - one entry per idea, same "
    "order, no markdown, no extra text. Do not invent field values, thresholds, or facts not present in "
    "the input."
)

# Keys that need a real connection selection in the UI; the model can't know these, so they are
# excluded from the example shape shown to it and are left for the user to fill in on the form.
_DRAFT_SHAPE_EXCLUDED_KEYS = {"target_connection_id", "target_kind", "target_name"}
DRAFT_CONFIG_SHAPES: dict[RuleType, dict[str, Any]] = {
    rule_type: {key: value for key, value in example.items() if key not in _DRAFT_SHAPE_EXCLUDED_KEYS}
    for rule_type, example in RULE_CONFIG_EXAMPLES.items()
}


class OllamaService:
    """Client for the shared Ollama endpoint, reached through Cloudflare Access.

    The endpoint sits behind Cloudflare Access, authenticated with a service token (a
    CF-Access-Client-Id / CF-Access-Client-Secret header pair), rather than being a purely
    local process - so unlike a plain localhost Ollama, requests do leave this machine.
    """

    def __init__(self, endpoint: str | None = None, model: str | None = None) -> None:
        settings = load_settings()
        self.endpoint = (endpoint or settings.get("ollama_endpoint") or DEFAULT_ENDPOINT).rstrip("/")
        self.model = model or settings.get("ollama_model") or DEFAULT_MODEL

    def _headers(self) -> dict[str, str]:
        # Cloudflare's bot protection blocks Python's default urllib User-Agent outright,
        # so every request must identify itself as the app instead.
        headers = {"Content-Type": "application/json", "User-Agent": "DQTool/0.1"}
        credentials = get_ollama_access_credentials()
        if credentials:
            client_id, client_secret = credentials
            headers["CF-Access-Client-Id"] = client_id
            headers["CF-Access-Client-Secret"] = client_secret
        return headers

    def is_available(self) -> bool:
        return self.check_connection()[0]

    def check_connection(self) -> tuple[bool, str]:
        """Probe the endpoint and describe the outcome, distinguishing auth rejection from downtime."""
        request = urllib.request.Request(f"{self.endpoint}/api/tags", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return True, f"Connected to {self.endpoint}."
                return False, f"{self.endpoint} answered HTTP {response.status}."
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return False, (
                    f"{self.endpoint} rejected the request (HTTP {exc.code}). The Cloudflare Access service "
                    "token is missing, wrong, or the Access application has no Service Auth policy for it."
                )
            return False, f"{self.endpoint} answered HTTP {exc.code}."
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, f"{self.endpoint} is not reachable: {exc}"

    def chat(self, prompt: str, system: str | None = None, timeout: float = 120.0) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "stream": False, "messages": messages}
        request = urllib.request.Request(
            f"{self.endpoint}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(
                    f"Ollama returned HTTP {exc.code}. Cloudflare Access rejected the request - check the "
                    "CF-Access-Client-Id / CF-Access-Client-Secret service token in AI settings."
                ) from None
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            if detail:
                raise RuntimeError(f"Ollama: {detail}") from None
            raise RuntimeError(
                f"Ollama returned HTTP {exc.code}. Check that model '{self.model}' is pulled (ollama list)."
            ) from None
        return str(data.get("message", {}).get("content", "")).strip()

    def explain_anomalies(
        self,
        source_label: str,
        profile: dict[str, Any],
        anomalies: list[dict[str, Any]],
    ) -> str:
        return self.chat(self._analysis_prompt(source_label, profile, anomalies), system=EXPLANATION_SYSTEM_PROMPT)

    def recommend_actions(
        self,
        source_label: str,
        profile: dict[str, Any],
        anomalies: list[dict[str, Any]],
        existing_rule_ideas: list[dict[str, Any]],
    ) -> str:
        prompt = json.loads(self._analysis_prompt(source_label, profile, anomalies))
        prompt["existing_rule_ideas"] = [
            {
                "name": idea.get("name"),
                "field": idea.get("column"),
                "rule_type": idea.get("rule_type"),
            }
            for idea in existing_rule_ideas
        ]
        return self.chat(json.dumps(prompt, default=str), system=RECOMMENDATION_SYSTEM_PROMPT)

    def explain_rule_failure(
        self,
        rule_name: str,
        rule_type: str,
        config: dict[str, Any],
        summary: dict[str, Any],
        status: str,
    ) -> str:
        safe_config = {key: value for key, value in config.items() if key not in {"source_connection_id", "target_connection_id"}}
        prompt = json.dumps(
            {
                "rule_name": rule_name,
                "rule_type": rule_type,
                "config": safe_config,
                "status": status,
                "checked_count": summary.get("checked_count"),
                "failed_count": summary.get("failed_count"),
                "fail_threshold_allowed": summary.get("fail_threshold_allowed"),
                "error": summary.get("error"),
            },
            default=str,
        )
        return self.chat(prompt, system=FAILURE_SYSTEM_PROMPT)

    def draft_rule(
        self,
        description: str,
        available_fields: list[str],
        sample_values: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Ask the model to both choose a rule type and draft its settings, grounded in real field names.

        sample_values is opt-in (off by default in the UI): when provided, it is sent to Ollama as-is, so
        callers must only pass it when the user explicitly consented to sharing source values.
        """
        catalog = [
            {
                "rule_type": rule_type.value,
                "label": RULE_TEMPLATES[rule_type]["name"],
                "description": RULE_TEMPLATES[rule_type]["description"],
                "config_shape_example": DRAFT_CONFIG_SHAPES.get(rule_type, {}),
            }
            for rule_type in RuleType
        ]
        prompt_payload: dict[str, Any] = {
            "user_description": description,
            "available_fields": available_fields,
            "rule_type_catalog": catalog,
        }
        if sample_values:
            prompt_payload["sample_values_by_field"] = sample_values
        draft = self._parse_json_object(self.chat(json.dumps(prompt_payload, default=str), system=RULE_DRAFT_SYSTEM_PROMPT))
        try:
            RuleType(str(draft.get("rule_type")))
        except ValueError:
            raise RuntimeError(f"The local model chose an unknown rule type: {draft.get('rule_type')!r}") from None
        return draft

    def prioritize_suggestions(
        self,
        source_label: str,
        profile: dict[str, Any],
        suggestions: list[dict[str, Any]],
    ) -> dict[int, dict[str, str]]:
        prompt = json.dumps(
            {
                "source": source_label,
                "row_count": profile.get("row_count"),
                "suggestions": [
                    {
                        "index": index,
                        "rule_type": suggestion.get("rule_type"),
                        "field": suggestion.get("column"),
                        "heuristic_reason": suggestion.get("reason"),
                    }
                    for index, suggestion in enumerate(suggestions)
                ],
            },
            default=str,
        )
        parsed = self._parse_json_array(self.chat(prompt, system=PRIORITIZE_SYSTEM_PROMPT))
        notes: dict[int, dict[str, str]] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= index < len(suggestions)):
                continue
            priority = str(item.get("priority", "")).strip().title()
            if priority not in {"High", "Medium", "Low"}:
                continue
            notes[index] = {"priority": priority, "why": str(item.get("why", "")).strip()}
        return notes

    def _strip_think_blocks(self, text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def _extract_json(self, text: str, open_char: str, close_char: str) -> str:
        cleaned = self._strip_think_blocks(text)
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned[:4].lower() == "json":
                cleaned = cleaned[4:]
        start = cleaned.find(open_char)
        end = cleaned.rfind(close_char)
        if start == -1 or end == -1 or end < start:
            raise RuntimeError("The local model did not return the expected JSON.")
        return cleaned[start : end + 1]

    def _parse_json_object(self, text: str) -> dict[str, Any]:
        try:
            data = json.loads(self._extract_json(text, "{", "}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"The local model returned invalid JSON: {exc}") from None
        if not isinstance(data, dict):
            raise RuntimeError("The local model's JSON response was not an object.")
        return data

    def _parse_json_array(self, text: str) -> list[Any]:
        try:
            data = json.loads(self._extract_json(text, "[", "]"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"The local model returned invalid JSON: {exc}") from None
        if not isinstance(data, list):
            raise RuntimeError("The local model's JSON response was not an array.")
        return data

    def _analysis_prompt(self, source_label: str, profile: dict[str, Any], anomalies: list[dict[str, Any]]) -> str:
        """Build a metadata-only prompt; source values and example values are intentionally excluded."""
        return json.dumps(
            {
                "source": source_label,
                "row_count": profile.get("row_count"),
                "columns": {
                    name: {
                        "type": stats.get("type"),
                        "inferred_type": stats.get("inferred_type"),
                        "null_rate": stats.get("null_rate"),
                        "distinct_count": stats.get("distinct_count"),
                        "min": stats.get("min"),
                        "max": stats.get("max"),
                        "mean": stats.get("mean"),
                    }
                    for name, stats in profile.get("columns", {}).items()
                },
                # Finding messages can contain example source values, so only pass their metadata.
                "findings": [
                    {"severity": finding.get("severity"), "column": finding.get("column")}
                    for finding in anomalies
                ],
                "gdpr_review_flags": [
                    {
                        "severity": finding.get("severity"),
                        "column": finding.get("column"),
                        "category": finding.get("category"),
                    }
                    for finding in profile.get("gdpr_findings") or []
                ],
            },
            default=str,
        )
