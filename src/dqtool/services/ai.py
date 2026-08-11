from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from dqtool.models.entities import RuleType
from dqtool.services.project import get_ollama_access_credentials, load_settings
from dqtool.services.rules import RULE_CONFIG_EXAMPLES, RULE_TEMPLATES

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:8b"
DRAFT_TIMEOUT_SECONDS = 300.0
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_DATE_BOUNDARY_PATTERN = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b")

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
    "You are a senior data quality analyst. You receive safe metadata about one rule execution: the rule "
    "type and settings, run counts, threshold, source label, and rule-specific summary facts such as a "
    "freshness timestamp or execution error. Return useful, concise Markdown (at most 300 words) with "
    "these headings: 'What happened', 'Evidence', 'Likely cause', and 'Recommended next steps'. Explain "
    "exactly what the configured rule checks before interpreting the result. Include all supplied relevant "
    "counts, bounds, timestamps, and threshold in Evidence. Give 2-4 actionable next steps appropriate "
    "to the rule type. Clearly label uncertainty. Do not invent source values, causes, schema, or business "
    "context that are not implied by the input, and never request or expose credentials."
)

RULE_DRAFT_SYSTEM_PROMPT = (
    "You are a data quality analyst assistant. A user describes, in plain English, a data-quality check "
    "they want on a chosen data source. You are given the exact field names available on that source and "
    "a catalog of supported rule types, each with an id, a short description, and an example JSON settings "
    "shape. Choose the single rule type from the catalog that best matches the description, then draft its "
    "settings. Return ONLY a single JSON object (no markdown, no commentary, no <think> blocks) with three "
    "keys: \"rule_type\" (the exact id of the chosen type from the catalog), \"name\" (a short rule name, "
    "at most 8 words), and \"config\" (an object using only the keys shown in that type's example shape). "
    "The user's description is authoritative: choose the rule type that directly implements that request, "
    "and never substitute an unrelated check merely because the sample data suggests one. Use a field name "
    "only from the provided field list; never invent a field name that is not in that list. Do not invent "
    "connection details, table names, or numeric thresholds the user did not imply. "
    "If nothing matches well, choose the closest reasonable rule type. When sample values are provided for "
    "a field, you may use them to infer its shape or meaning - for example to write a matching regex, spot "
    "a plausible numeric range, or list a field's current set of values for an allowed-values check - but "
    "only draw config values from what is actually shown to you, never invent beyond it. Sample values may "
    "help configure the requested check, but they must not change which check the user requested."
    " For a date requirement that says a field must be on/after, after, on/before, or before a stated date, "
    "choose date_validity and set min_date and/or max_date to that ISO date (YYYY-MM-DD). For strictly "
    "'after' or 'before' wording, set the bound to the next or preceding day respectively."
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
    """Client for a local or remote Ollama endpoint.

    Remote endpoints can be protected by Cloudflare Access using a service token (a
    CF-Access-Client-Id / CF-Access-Client-Secret header pair). The default endpoint is
    local, so requests stay on this machine unless a remote endpoint is configured.
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

    def chat(
        self,
        prompt: str,
        system: str | None = None,
        timeout: float = 120.0,
        *,
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "stream": False, "messages": messages}
        if json_schema is not None:
            payload["format"] = json_schema
            payload["think"] = False
            payload["options"] = {"temperature": 0}
        elif json_mode:
            payload["format"] = "json"
            payload["think"] = False
            payload["options"] = {"temperature": 0}
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
        except TimeoutError:
            raise RuntimeError(
                f"Ollama timed out after {timeout:g} seconds while using '{self.model}'. "
                "Try again without sample values or choose a smaller model in AI settings."
            ) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise RuntimeError(
                    f"Ollama timed out after {timeout:g} seconds while using '{self.model}'. "
                    "Try again without sample values or choose a smaller model in AI settings."
                ) from None
            raise RuntimeError(f"Ollama is not reachable at {self.endpoint}: {exc.reason}") from None
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
        private_config_keys = {
            "source_connection_id", "source_kind", "source_name", "source_sql",
            "target_connection_id", "target_kind", "target_name", "target_sql",
        }
        safe_config = {key: value for key, value in config.items() if key not in private_config_keys}
        summary_fields = {
            "source_label": summary.get("source_label"),
            "checked_count": summary.get("checked_count"),
            "failed_count": summary.get("failed_count"),
            "fail_threshold_allowed": summary.get("fail_threshold_allowed"),
            "latest_value": summary.get("latest_value"),
            "freshness_age_days": summary.get("freshness_age_days"),
            "max_age_days": summary.get("max_age_days"),
            "freshness_message": summary.get("freshness_message"),
            "error": summary.get("error"),
        }
        prompt = json.dumps(
            {
                "rule_name": rule_name,
                "rule_type": rule_type,
                "config": safe_config,
                "status": status,
                "run_summary": {key: value for key, value in summary_fields.items() if value is not None},
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
        constrained_types = self._draft_rule_type_constraint(description)
        date_boundary = self._date_boundary_from_description(description)
        prompt_payload: dict[str, Any] = {
            "user_description": description,
            "available_fields": available_fields,
            "rule_type_catalog": catalog,
        }
        if constrained_types:
            prompt_payload["required_rule_type"] = constrained_types[0].value
        if sample_values:
            prompt_payload["sample_values_by_field"] = sample_values
        response_schema = {
            "type": "object",
            "properties": {
                "rule_type": {
                    "type": "string",
                    "enum": [rule_type.value for rule_type in (constrained_types or list(RuleType))],
                },
                "name": {"type": "string"},
                "config": {"type": "object"},
            },
            "required": ["rule_type", "name", "config"],
            "additionalProperties": False,
        }
        draft = self._parse_json_object(
            self.chat(
                json.dumps(prompt_payload, default=str),
                system=RULE_DRAFT_SYSTEM_PROMPT,
                timeout=DRAFT_TIMEOUT_SECONDS,
                json_mode=True,
                json_schema=response_schema,
            )
        )
        config = draft.get("config")
        if not isinstance(config, dict):
            raise RuntimeError("The local model did not return rule settings as a JSON object.")
        if constrained_types == [RuleType.REGEX]:
            email_fields = [field for field in available_fields if "email" in field.casefold() or "e-mail" in field.casefold()]
            if not email_fields:
                raise RuntimeError("The request is for email validation, but the selected source has no email field.")
            config["column"] = email_fields[0]
            config["pattern"] = EMAIL_PATTERN
        elif constrained_types == [RuleType.DATE_VALIDITY] and date_boundary:
            bound_key, bound_date = date_boundary
            config.pop("min_date", None)
            config.pop("max_date", None)
            config[bound_key] = bound_date
        self._validate_draft_fields(config, available_fields)
        try:
            RuleType(str(draft.get("rule_type")))
        except ValueError:
            raise RuntimeError(f"The local model chose an unknown rule type: {draft.get('rule_type')!r}") from None
        return draft

    def _validate_draft_fields(self, config: dict[str, Any], available_fields: list[str]) -> None:
        """Reject invented field names before opening a form with an empty selection."""
        referenced = []
        for key in ("column", "source_key", "key_column"):
            if config.get(key):
                referenced.append(str(config[key]))
        referenced.extend(str(value) for value in config.get("columns", []) or [])
        referenced.extend(str(value) for value in config.get("compare_columns", []) or [])
        missing = [field for field in referenced if field not in available_fields]
        if missing:
            raise RuntimeError(f"The local model selected unavailable field(s): {', '.join(missing)}.")

    def _draft_rule_type_constraint(self, description: str) -> list[RuleType]:
        """Constrain unambiguous user requests so sample data cannot redirect the draft."""
        normalized = description.casefold()
        asks_about_email = "email" in normalized or "e-mail" in normalized
        asks_about_validity = any(word in normalized for word in ("correct", "valid", "format", "address"))
        if asks_about_email and asks_about_validity:
            return [RuleType.REGEX]
        if self._date_boundary_from_description(description):
            return [RuleType.DATE_VALIDITY]
        return []

    def _date_boundary_from_description(self, description: str) -> tuple[str, str] | None:
        """Extract an explicit date boundary from common plain-English date requirements."""
        normalized = description.casefold()
        for match in _DATE_BOUNDARY_PATTERN.finditer(normalized):
            raw_date = match.group(1)
            parsed = None
            for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(raw_date, pattern).date()
                    break
                except ValueError:
                    continue
            if parsed is None:
                continue
            context = normalized[max(0, match.start() - 40) : match.end() + 20]
            following_text = normalized[match.end() : match.end() + 50]
            makes_dates_invalid = any(
                phrase in following_text
                for phrase in ("invalid", "not valid", "must fail", "should fail", "not allowed")
            )
            if makes_dates_invalid and any(phrase in context for phrase in ("on or after", "after", "since", "not before")):
                maximum = parsed - timedelta(days=1) if "on or after" in context else parsed
                return "max_date", maximum.isoformat()
            if makes_dates_invalid and any(phrase in context for phrase in ("on or before", "before", "until", "not after")):
                minimum = parsed + timedelta(days=1) if "on or before" in context else parsed
                return "min_date", minimum.isoformat()
            if any(phrase in context for phrase in ("on or after", "since", "not before")):
                return "min_date", parsed.isoformat()
            if any(phrase in context for phrase in ("after", "later than")):
                return "min_date", (parsed + timedelta(days=1)).isoformat()
            if any(phrase in context for phrase in ("on or before", "until", "not after")):
                return "max_date", parsed.isoformat()
            if any(phrase in context for phrase in ("before", "earlier than")):
                return "max_date", (parsed - timedelta(days=1)).isoformat()
        return None

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
        response_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0, "maximum": max(len(suggestions) - 1, 0)},
                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "why": {"type": "string"},
                },
                "required": ["index", "priority", "why"],
                "additionalProperties": False,
            },
            "minItems": len(suggestions),
            "maxItems": len(suggestions),
        }
        parsed = self._parse_json_array(
            self.chat(prompt, system=PRIORITIZE_SYSTEM_PROMPT, json_mode=True, json_schema=response_schema)
        )
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
