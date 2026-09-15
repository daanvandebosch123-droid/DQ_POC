from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from dqtool.services.profiling import frequency_analysis_status

HEADER_FILL = PatternFill("solid", fgColor="5B5248")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def build_anomaly_report_workbook(
    source_label: str, profile: dict[str, Any], anomalies: list[dict[str, Any]]
) -> bytes:
    """Create an Excel workbook for an anomaly profile without including source rows."""
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    _append_sheet(
        summary,
        ["Metric", "Value"],
        [
            ("Source", source_label),
            ("Profiled at (UTC)", profile.get("profiled_at") or ""),
            ("Rows", int(profile.get("row_count") or 0)),
            ("Columns profiled", len(profile.get("columns") or {})),
            ("Text inference rows scanned", profile.get("text_inference_rows") or ""),
            (
                "Text inference mode",
                "Not applicable"
                if profile.get("text_inference_rows") is None
                else ("Sampled" if profile.get("text_inference_sampled") else "Full source"),
            ),
            ("Drift findings", len(anomalies)),
            ("High-severity findings", sum(1 for finding in anomalies if finding.get("severity") == "high")),
            ("GDPR review flags", len(profile.get("gdpr_findings") or [])),
        ],
    )

    findings = workbook.create_sheet("Drift findings")
    _append_sheet(
        findings,
        ["Severity", "Column", "Finding"],
        [
            (str(finding.get("severity") or "").upper(), finding.get("column") or "-", finding.get("message") or "")
            for finding in anomalies
        ],
    )

    columns = workbook.create_sheet("Column profile")
    _append_sheet(
        columns,
        [
            "Field",
            "Type",
            "Meaning",
            "Inference match %",
            "Inference values",
            "Inference rows",
            "Inference scope",
            "SQL Null %",
            "Blank %",
            "Distinct",
            "Min",
            "Max",
            "Mean",
            "Frequency analysis",
        ],
        [
            (
                name,
                stats.get("type") or "",
                stats.get("inferred_type") or "text",
                _percent(stats.get("inference_confidence")),
                stats.get("inference_sample_size") if stats.get("inference_sample_size") is not None else "",
                stats.get("inference_rows_scanned") if stats.get("inference_rows_scanned") is not None else "",
                (
                    ""
                    if stats.get("inference_rows_scanned") is None
                    else ("Sampled" if stats.get("inference_sampled") else "Full source")
                ),
                _percent(stats.get("null_rate")),
                _percent(stats.get("blank_rate")),
                stats.get("distinct_count") if stats.get("distinct_count") is not None else "",
                stats.get("min") if stats.get("min") is not None else "",
                stats.get("max") if stats.get("max") is not None else "",
                stats.get("mean") if stats.get("mean") is not None else "",
                frequency_analysis_status(stats),
            )
            for name, stats in profile.get("columns", {}).items()
        ],
    )

    frequencies = workbook.create_sheet("Value frequencies")
    _append_sheet(
        frequencies,
        ["Field", "Value", "Count", "Share"],
        [
            (name, item["value"], item["count"], item["share"])
            for name, stats in profile.get("columns", {}).items()
            for item in sorted(
                stats.get("frequency_values") or stats.get("top_values") or [],
                key=lambda item: (-int(item.get("count") or 0), str(item.get("value") or "")),
            )
        ],
    )

    gdpr = workbook.create_sheet("GDPR review")
    _append_sheet(
        gdpr,
        ["Severity", "Column", "Category", "Reason"],
        [
            (
                str(finding.get("severity") or "").upper(),
                finding.get("column") or "",
                finding.get("category") or "",
                finding.get("reason") or "",
            )
            for finding in profile.get("gdpr_findings") or []
        ],
    )

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _append_sheet(sheet, headers: list[str], rows: list[tuple[Any, ...]]) -> None:
    safe_headers = [_safe_cell_value(header) for header in headers]
    safe_rows = [tuple(_safe_cell_value(value) for value in row) for row in rows]
    sheet.append(safe_headers)
    for cell in sheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, len(safe_rows) + 1)}"
    for row_index, row in enumerate(safe_rows, start=2):
        sheet.append(row)
        for column_index, value in enumerate(row, start=1):
            if isinstance(value, str):
                # Preserve literal source text, including '=' and '#N/A' values.
                sheet.cell(row=row_index, column=column_index).data_type = "s"
    for index, header in enumerate(safe_headers, start=1):
        values = [str(header), *(str(row[index - 1] or "") for row in safe_rows)]
        sheet.column_dimensions[get_column_letter(index)].width = min(60, max(12, max(map(len, values)) + 2))


def _safe_cell_value(value: Any) -> Any:
    """Remove control characters that Excel cells cannot represent."""
    return ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value


def _percent(value: Any) -> float | str:
    return "" if value is None else float(value)
