from __future__ import annotations

import hashlib
from typing import Any

import duckdb

from dqtool.models.entities import Connection, ConnectionType, utc_now
from dqtool.services.connectors import ConnectorService

MAX_PROFILED_COLUMNS = 50

ROW_COUNT_HIGH = 0.30
ROW_COUNT_MEDIUM = 0.10
NULL_RATE_HIGH = 0.10
NULL_RATE_MEDIUM = 0.02
DISTINCT_DROP_RATIO = 0.5
MEAN_SHIFT_STDDEVS = 3.0

DOMINANT_SHARE = 0.8
MAX_EXAMPLE_VALUES = 5
OUTLIER_FENCE_MULTIPLIER = 1.5
EMAIL_LOOSE_PATTERN = "^[^@\\s]+@[^@\\s]+$"
EMAIL_STRICT_PATTERN = "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$"


def source_profile_key(source_config: dict[str, Any]) -> str:
    """Stable identity for a rule source, used to link profile snapshots over time."""
    connection_id = source_config.get("source_connection_id")
    kind = source_config.get("source_kind") or ""
    name = str(source_config.get("source_name") or "").strip()
    if not name:
        sql = str(source_config.get("source_sql") or "").strip()
        name = hashlib.sha1(sql.encode("utf-8")).hexdigest()[:12]
    return f"{connection_id}:{kind}:{name}"


class ProfilingService:
    def __init__(self, connector_service: ConnectorService) -> None:
        self.connector_service = connector_service

    def profile_rule_source(
        self,
        source_config: dict[str, Any],
        connections: dict[int, Connection],
    ) -> dict[str, Any]:
        connection = connections[int(source_config["source_connection_id"])]
        if connection.connection_type == ConnectionType.CSV:
            return self._profile_duckdb(source_config, connections)
        return self._profile_oracle(source_config, connection)

    def _profile_duckdb(
        self,
        source_config: dict[str, Any],
        connections: dict[int, Connection],
    ) -> dict[str, Any]:
        con = duckdb.connect()
        try:
            relation = self.connector_service.build_rule_source_relation(con, source_config, connections)
            con.sql(f"CREATE OR REPLACE VIEW profile_view AS {relation.sql_query()}")
            summary = con.execute("SUMMARIZE SELECT * FROM profile_view").fetchall()
            summary_columns = [column[0] for column in con.execute("SUMMARIZE SELECT * FROM profile_view").description]
            index = {name: position for position, name in enumerate(summary_columns)}
            row_count = con.execute("SELECT COUNT(*) FROM profile_view").fetchone()[0]
            columns: dict[str, dict[str, Any]] = {}
            numeric_quartiles: list[tuple[str, float, float]] = []
            for row in summary[:MAX_PROFILED_COLUMNS]:
                name = row[index["column_name"]]
                null_percentage = row[index["null_percentage"]]
                columns[name] = {
                    "type": str(row[index["column_type"]]),
                    "null_rate": round(float(null_percentage or 0) / 100.0, 6),
                    "min": self._json_safe(row[index["min"]]),
                    "max": self._json_safe(row[index["max"]]),
                    "mean": self._to_float(row[index["avg"]]),
                    "stddev": self._to_float(row[index["std"]]),
                }
                q25 = self._to_float(row[index["q25"]])
                q75 = self._to_float(row[index["q75"]])
                if q25 is not None and q75 is not None:
                    numeric_quartiles.append((name, q25, q75))
            # SUMMARIZE only offers approx_unique, which can exceed the row count; count exactly instead.
            if columns:
                quoted = ['"' + name.replace('"', '""') + '"' for name in columns]
                distinct_counts = con.execute(
                    f"SELECT {', '.join(f'COUNT(DISTINCT {column})' for column in quoted)} FROM profile_view"
                ).fetchone()
                for name, distinct in zip(columns, distinct_counts, strict=True):
                    columns[name]["distinct_count"] = int(distinct or 0)
            findings = self._content_findings_duckdb(con, columns, numeric_quartiles)
            return {
                "profiled_at": utc_now(),
                "row_count": int(row_count),
                "columns": columns,
                "content_findings": findings,
            }
        finally:
            con.close()

    def _content_findings_duckdb(
        self,
        con: duckdb.DuckDBPyConnection,
        columns: dict[str, dict[str, Any]],
        numeric_quartiles: list[tuple[str, float, float]],
    ) -> list[dict[str, Any]]:
        """Flag odd values inside the current snapshot: mixed types, malformed emails, outliers."""
        findings: list[dict[str, Any]] = []
        for name, stats in columns.items():
            if not str(stats.get("type", "")).upper().startswith("VARCHAR"):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            non_null, numeric, email_loose, email_strict = con.execute(
                f"SELECT COUNT({quoted}), "
                f"COUNT(*) FILTER (WHERE try_cast({quoted} AS DOUBLE) IS NOT NULL), "
                f"COUNT(*) FILTER (WHERE regexp_matches({quoted}, '{EMAIL_LOOSE_PATTERN}')), "
                f"COUNT(*) FILTER (WHERE regexp_matches({quoted}, '{EMAIL_STRICT_PATTERN}')) "
                f"FROM profile_view"
            ).fetchone()
            if not non_null:
                continue
            numeric_share = numeric / non_null
            email_share = email_loose / non_null
            if email_share >= DOMINANT_SHARE:
                malformed = email_loose - email_strict
                if malformed:
                    examples = self._example_values(
                        con,
                        quoted,
                        f"regexp_matches({quoted}, '{EMAIL_LOOSE_PATTERN}') "
                        f"AND NOT regexp_matches({quoted}, '{EMAIL_STRICT_PATTERN}')",
                    )
                    findings.append(
                        _finding("medium", name, f"{malformed} email value(s) look malformed (no dot in the domain), e.g. {examples}.")
                    )
                other = non_null - email_loose
                if other:
                    examples = self._example_values(con, quoted, f"NOT regexp_matches({quoted}, '{EMAIL_LOOSE_PATTERN}')")
                    findings.append(
                        _finding(
                            "medium",
                            name,
                            f"{other} value(s) do not look like email addresses in a column that is {email_share:.0%} emails, e.g. {examples}.",
                        )
                    )
            elif 0 < numeric_share <= (1 - DOMINANT_SHARE):
                examples = self._example_values(con, quoted, f"try_cast({quoted} AS DOUBLE) IS NOT NULL")
                findings.append(
                    _finding("medium", name, f"{numeric} numeric-looking value(s) in a mostly text column, e.g. {examples}.")
                )
            elif DOMINANT_SHARE <= numeric_share < 1:
                non_numeric = non_null - numeric
                examples = self._example_values(con, quoted, f"try_cast({quoted} AS DOUBLE) IS NULL")
                findings.append(
                    _finding("medium", name, f"{non_numeric} non-numeric value(s) in a mostly numeric column, e.g. {examples}.")
                )
        for name, q25, q75 in numeric_quartiles:
            iqr = q75 - q25
            if iqr <= 0:
                continue
            low_fence = q25 - OUTLIER_FENCE_MULTIPLIER * iqr
            high_fence = q75 + OUTLIER_FENCE_MULTIPLIER * iqr
            quoted = '"' + name.replace('"', '""') + '"'
            condition = f"{quoted} < {low_fence} OR {quoted} > {high_fence}"
            count = con.execute(f"SELECT COUNT(*) FROM profile_view WHERE {condition}").fetchone()[0]
            if count:
                examples = self._example_values(con, quoted, condition)
                findings.append(
                    _finding(
                        "low",
                        name,
                        f"{count} numeric outlier(s) outside [{low_fence:.4g}, {high_fence:.4g}], e.g. {examples}.",
                    )
                )
        return findings

    def _example_values(self, con: duckdb.DuckDBPyConnection, quoted: str, condition: str) -> str:
        rows = con.execute(
            f"SELECT DISTINCT {quoted} FROM profile_view WHERE {quoted} IS NOT NULL AND ({condition}) LIMIT {MAX_EXAMPLE_VALUES}"
        ).fetchall()
        values = []
        for row in rows:
            text = str(row[0])
            if len(text) > 30:
                text = text[:27] + "..."
            values.append(f"'{text}'")
        return ", ".join(values) if values else "(none)"

    def _profile_oracle(self, source_config: dict[str, Any], connection: Connection) -> dict[str, Any]:
        sql = self.connector_service.rule_source_sql(source_config)
        dialect = self.connector_service.database_dialect(connection)
        db_conn = self.connector_service.connect_database(connection)
        numeric_markers = ("NUMBER", "FLOAT", "DECIMAL", "NUMERIC", "INT", "DOUBLE", "REAL")
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(self.connector_service.limited_sql(sql, 0, dialect))
                described = cursor.description[:MAX_PROFILED_COLUMNS]
                numeric_names = {
                    item[0]
                    for item in described
                    if any(marker in str(item[1]).upper() for marker in numeric_markers)
                }
                aggregates = ["COUNT(*)"]
                for item in described:
                    quoted = '"' + item[0].replace('"', '""') + '"'
                    aggregates.append(f"COUNT({quoted})")
                    aggregates.append(f"COUNT(DISTINCT {quoted})")
                    if item[0] in numeric_names:
                        aggregates.extend(
                            [f"MIN({quoted})", f"MAX({quoted})", f"AVG({quoted})", f"STDDEV({quoted})"]
                        )
                cursor.execute(f"SELECT {', '.join(aggregates)} FROM ({sql}) q")
                values = list(cursor.fetchone())
            row_count = int(values.pop(0))
            columns: dict[str, dict[str, Any]] = {}
            for item in described:
                non_null = int(values.pop(0))
                distinct = int(values.pop(0))
                stats: dict[str, Any] = {
                    "type": str(item[1]),
                    "null_rate": round(1 - (non_null / row_count), 6) if row_count else 0.0,
                    "distinct_count": distinct,
                    "min": None,
                    "max": None,
                    "mean": None,
                    "stddev": None,
                }
                if item[0] in numeric_names:
                    stats["min"] = self._json_safe(values.pop(0))
                    stats["max"] = self._json_safe(values.pop(0))
                    stats["mean"] = self._to_float(values.pop(0))
                    stats["stddev"] = self._to_float(values.pop(0))
                columns[item[0]] = stats
            return {"profiled_at": utc_now(), "row_count": row_count, "columns": columns}
        finally:
            db_conn.close()

    def _json_safe(self, value: Any) -> Any:
        if value is None or isinstance(value, (int, float, str, bool)):
            return value
        return str(value)

    def _to_float(self, value: Any) -> float | None:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None


def detect_anomalies(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    """Return findings for the current snapshot: content oddities plus drift versus the previous one."""
    findings: list[dict[str, Any]] = list(current.get("content_findings") or [])
    if previous is None:
        return findings

    prev_rows = int(previous.get("row_count") or 0)
    cur_rows = int(current.get("row_count") or 0)
    if prev_rows:
        change = (cur_rows - prev_rows) / prev_rows
        if abs(change) >= ROW_COUNT_HIGH:
            findings.append(_finding("high", None, f"Row count changed {change:+.0%} ({prev_rows:,} to {cur_rows:,})."))
        elif abs(change) >= ROW_COUNT_MEDIUM:
            findings.append(_finding("medium", None, f"Row count changed {change:+.0%} ({prev_rows:,} to {cur_rows:,})."))

    prev_columns: dict[str, dict[str, Any]] = previous.get("columns", {})
    cur_columns: dict[str, dict[str, Any]] = current.get("columns", {})

    for name in sorted(prev_columns.keys() - cur_columns.keys()):
        findings.append(_finding("medium", name, "Column disappeared from the source."))
    for name in sorted(cur_columns.keys() - prev_columns.keys()):
        findings.append(_finding("low", name, "New column appeared in the source."))

    for name in sorted(prev_columns.keys() & cur_columns.keys()):
        prev_col = prev_columns[name]
        cur_col = cur_columns[name]

        prev_null = float(prev_col.get("null_rate") or 0)
        cur_null = float(cur_col.get("null_rate") or 0)
        if cur_null - prev_null >= NULL_RATE_HIGH:
            findings.append(_finding("high", name, f"Null rate jumped from {prev_null:.1%} to {cur_null:.1%}."))
        elif cur_null - prev_null >= NULL_RATE_MEDIUM and cur_null > 3 * max(prev_null, 0.001):
            findings.append(_finding("medium", name, f"Null rate rose from {prev_null:.1%} to {cur_null:.1%}."))

        prev_distinct = prev_col.get("distinct_count")
        cur_distinct = cur_col.get("distinct_count")
        if prev_distinct and cur_distinct is not None and cur_distinct < prev_distinct * DISTINCT_DROP_RATIO:
            findings.append(
                _finding("medium", name, f"Distinct values dropped from {prev_distinct:,} to {cur_distinct:,}.")
            )

        prev_mean = prev_col.get("mean")
        cur_mean = cur_col.get("mean")
        prev_std = prev_col.get("stddev")
        if prev_mean is not None and cur_mean is not None and prev_std:
            if abs(cur_mean - prev_mean) > MEAN_SHIFT_STDDEVS * prev_std:
                findings.append(
                    _finding(
                        "medium",
                        name,
                        f"Mean shifted from {prev_mean:.4g} to {cur_mean:.4g} (more than {MEAN_SHIFT_STDDEVS:.0f} standard deviations).",
                    )
                )

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda item: order[item["severity"]])
    return findings


def _finding(severity: str, column: str | None, message: str) -> dict[str, Any]:
    return {"severity": severity, "column": column, "message": message}
