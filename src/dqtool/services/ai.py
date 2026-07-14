from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from dqtool.services.project import load_settings

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"

SYSTEM_PROMPT = (
    "You are a data quality analyst. You receive column statistics and drift findings "
    "for a dataset. Explain what stands out and the most likely causes in plain language. "
    "Be concise (at most 150 words), factual, and do not invent columns or numbers that "
    "are not in the input. If there are no findings, say the data looks stable."
)


class OllamaService:
    """Minimal client for a locally running Ollama server. No data leaves the machine."""

    def __init__(self, endpoint: str | None = None, model: str | None = None) -> None:
        settings = load_settings()
        self.endpoint = (endpoint or settings.get("ollama_endpoint") or DEFAULT_ENDPOINT).rstrip("/")
        self.model = model or settings.get("ollama_model") or DEFAULT_MODEL

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.endpoint}/api/tags", timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def chat(self, prompt: str, system: str | None = None, timeout: float = 120.0) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "stream": False, "messages": messages}
        request = urllib.request.Request(
            f"{self.endpoint}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
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
        prompt = json.dumps(
            {
                "source": source_label,
                "row_count": profile.get("row_count"),
                "columns": {
                    name: {
                        "type": stats.get("type"),
                        "null_rate": stats.get("null_rate"),
                        "distinct_count": stats.get("distinct_count"),
                        "mean": stats.get("mean"),
                    }
                    for name, stats in profile.get("columns", {}).items()
                },
                "drift_findings": anomalies,
            },
            default=str,
        )
        return self.chat(prompt, system=SYSTEM_PROMPT)
