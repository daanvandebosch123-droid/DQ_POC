from __future__ import annotations

import csv
import math
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb

from dqtool.models.entities import Connection, Dataset, DatasetType, Rule, RuleRun, RuleType, utc_now
from dqtool.services.connectors import ConnectorService
from dqtool.services.rules import normalize_rule_config, validate_rule_config

LOCAL_DATASET_TYPES = {DatasetType.CSV_FILE, DatasetType.CSV_FOLDER_FILE, DatasetType.APP_JOIN}
FAILED_ROW_LIMIT = 500
FETCH_BATCH_SIZE = 5000
KEY_FETCH_BATCH_SIZE = 10000

# Maps accepted operator spellings to SQL that is valid in both DuckDB and Oracle.
_COMPARISON_OPERATORS = {
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "==": "=",
    "=": "=",
    "!=": "<>",
    "<>": "<>",
}

RelationBuilder = Callable[[duckdb.DuckDBPyConnection], Any]
RowBatches = Iterator[tuple[list[str], list[tuple[Any, ...]]]]


class ExecutionService:
    def __init__(self, connector_service: ConnectorService) -> None:
        self.connector_service = connector_service

    def run_rules(
        self,
        rules: list[Rule],
        datasets: dict[int, Dataset],
        connections: dict[int, Connection],
        results_dir: Path,
        executed_by: str,
    ) -> list[RuleRun]:
        runs: list[RuleRun] = []
        for rule in rules:
            started_at = utc_now()
            dataset_id = rule.dataset_id or 0
            failed_rows_path = None
            source_label = self._source_label(rule)
            try:
                summary, failed_rows = self._execute_rule(rule, datasets, connections)
                summary["source_label"] = source_label
                if failed_rows:
                    results_dir.mkdir(parents=True, exist_ok=True)
                    failed_rows_path = str(results_dir / self._failed_rows_filename(rule))
                    self._write_failed_rows(Path(failed_rows_path), failed_rows, started_at)
                allowed, status = self._status_for_counts(
                    rule.config, summary["checked_count"], summary["failed_count"]
                )
                if allowed:
                    summary["fail_threshold_allowed"] = allowed
            except Exception as exc:
                status = "error"
                summary = {
                    "checked_count": 0,
                    "failed_count": 0,
                    "rule_type": rule.rule_type.value,
                    "source_label": source_label,
                    "error": str(exc),
                }
            runs.append(
                RuleRun(
                    id=None,
                    rule_id=rule.id or 0,
                    dataset_id=dataset_id,
                    status=status,
                    executed_by=executed_by,
                    started_at=started_at,
                    finished_at=utc_now(),
                    summary_json=summary,
                    failed_rows_path=failed_rows_path,
                )
            )
        return runs

    def _execute_rule(
        self,
        rule: Rule,
        datasets: dict[int, Dataset],
        connections: dict[int, Connection],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if rule.dataset_id is None:
            errors = validate_rule_config(rule.rule_type, rule.config, require_source=True)
            if errors:
                raise ValueError(f"Invalid rule configuration. {'; '.join(errors)}")
            source_connection_id = int(rule.config["source_connection_id"])
            if source_connection_id not in connections:
                raise ValueError("The selected source connection no longer exists or is not accessible.")
            self._validate_connection_kind(connections[source_connection_id], rule.config.get("source_kind"), "source")
            if rule.rule_type == RuleType.REFERENTIAL_INTEGRITY:
                target_connection_id = int(rule.config["target_connection_id"])
                if target_connection_id not in connections:
                    raise ValueError("The selected target connection no longer exists or is not accessible.")
                self._validate_connection_kind(connections[target_connection_id], rule.config.get("target_kind"), "target")

        if self._uses_embedded_source(rule):
            if rule.rule_type == RuleType.REFERENTIAL_INTEGRITY:
                target_source = self._extract_target_source_config(rule.config)
                return self._execute_referential_integrity_rule_sources(rule, rule.config, target_source, connections)
            return self._execute_rule_source(rule, rule.config, connections)

        dataset = datasets[rule.dataset_id or 0]
        if rule.rule_type == RuleType.REFERENTIAL_INTEGRITY:
            target_dataset_id = int(rule.config.get("target_dataset_id", 0))
            target_dataset = datasets.get(target_dataset_id)
            if target_dataset is None:
                raise ValueError("Select a valid target source for the referential integrity rule.")
            return self._execute_referential_integrity(rule, dataset, target_dataset, connections)
        if dataset.dataset_type in LOCAL_DATASET_TYPES:
            return self._run_duckdb_rule(rule, self._dataset_relation_builder(dataset, connections))
        return self._run_oracle_rule(rule, connections[dataset.connection_id or 0], self._oracle_dataset_sql(dataset))

    def _execute_rule_source(
        self,
        rule: Rule,
        source_config: dict[str, Any],
        connections: dict[int, Connection],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        connection = connections[int(source_config["source_connection_id"])]
        if rule.rule_type == RuleType.CUSTOM_SQL_CONNECTION:
            return self._run_connection_sql_rule(rule, connection)
        if connection.connection_type.value == "csv":
            return self._run_duckdb_rule(rule, self._rule_source_relation_builder(source_config, connections))
        return self._run_oracle_rule(rule, connection, self.connector_service.oracle_rule_source_sql(source_config))

    def _run_connection_sql_rule(self, rule: Rule, connection: Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run rule SQL against the whole connection instead of one selected table.

        Every returned row is a failed row. Because there is no single source relation,
        checked_count equals failed_count; use the row-count fail threshold for tolerances.
        """
        errors = validate_rule_config(rule.rule_type, rule.config)
        if errors:
            raise ValueError(f"Invalid {rule.rule_type.value} rule configuration. {'; '.join(errors)}")
        sql = str(rule.config["sql"]).strip().rstrip(";")
        if connection.connection_type.value == "csv":
            con = duckdb.connect()
            try:
                self.connector_service.register_connection_views(con, connection)
                cursor = con.execute(f"SELECT * FROM ({sql}) failed LIMIT {FAILED_ROW_LIMIT}")
                columns = [column[0] for column in cursor.description]
                failed_rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
                failed_count = con.execute(f"SELECT COUNT(*) FROM ({sql}) failed").fetchone()[0]
            finally:
                con.close()
        else:
            dialect = self.connector_service.database_dialect(connection)
            db_conn = self.connector_service.connect_database(connection)
            try:
                with db_conn.cursor() as cursor:
                    cursor.execute(self.connector_service.limited_sql(sql, FAILED_ROW_LIMIT, dialect))
                    columns = [column[0] for column in cursor.description]
                    failed_rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
                    cursor.execute(f"SELECT COUNT(*) FROM ({sql}) failed")
                    failed_count = cursor.fetchone()[0]
            finally:
                db_conn.close()
        return self._summary(rule, failed_count, failed_count), failed_rows

    # --- relation builders -------------------------------------------------

    def _dataset_relation_builder(self, dataset: Dataset, connections: dict[int, Connection]) -> RelationBuilder:
        return lambda con: self.connector_service._build_local_relation(con, dataset, connections)

    def _rule_source_relation_builder(self, source_config: dict[str, Any], connections: dict[int, Connection]) -> RelationBuilder:
        return lambda con: self.connector_service.build_rule_source_relation(con, source_config, connections)

    # --- unified rule execution --------------------------------------------

    def _run_duckdb_rule(self, rule: Rule, build_relation: RelationBuilder) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        con = duckdb.connect()
        try:
            relation = build_relation(con)
            relation_name = "dataset_view"
            con.sql(f"CREATE OR REPLACE VIEW {relation_name} AS {relation.sql_query()}")
            failed_sql, summary_sql = self._build_rule_sql(rule, relation_name, dialect="duckdb")
            cursor = con.execute(f"{failed_sql} LIMIT {FAILED_ROW_LIMIT}")
            columns = [column[0] for column in cursor.description]
            failed_preview = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
            summary_row = con.execute(summary_sql).fetchone()
            if summary_row is None:
                raise RuntimeError("The rule summary query returned no result.")
            return self._summary(rule, summary_row[0], summary_row[1]), failed_preview
        finally:
            con.close()

    def _run_oracle_rule(self, rule: Rule, connection: Connection, source_sql: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dialect = self.connector_service.database_dialect(connection)
        failed_sql, summary_sql = self._build_rule_sql(rule, f"({source_sql})", dialect=dialect)
        db_conn = self.connector_service.connect_database(connection)
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(self.connector_service.limited_sql(failed_sql, FAILED_ROW_LIMIT, dialect))
                columns = [column[0] for column in cursor.description]
                failed_rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
                cursor.execute(summary_sql)
                checked_count, failed_count = cursor.fetchone()
            return self._summary(rule, checked_count, failed_count), failed_rows
        finally:
            db_conn.close()

    def _status_for_counts(self, config: dict[str, Any], checked_count: Any, failed_count: Any) -> tuple[int, str]:
        """Decide pass/fail from failed_count against the rule's configured tolerance.

        fail_threshold_count and fail_threshold_percent are both optional and default to 0,
        which reproduces the original behavior (any failed row fails the rule). When either
        is set, the run still passes as long as failed_count does not exceed whichever
        allowance (row count, or percent of checked_count) is larger.
        """
        try:
            threshold_count = max(0, int(config.get("fail_threshold_count") or 0))
        except (TypeError, ValueError):
            threshold_count = 0
        try:
            threshold_percent = max(0.0, float(config.get("fail_threshold_percent") or 0))
        except (TypeError, ValueError):
            threshold_percent = 0.0
        checked = int(checked_count or 0)
        failed = int(failed_count or 0)
        allowed = threshold_count
        if threshold_percent > 0 and checked > 0:
            allowed = max(allowed, math.floor(checked * threshold_percent / 100))
        status = "passed" if failed <= allowed else "failed"
        return allowed, status

    def _summary(self, rule: Rule, checked_count: Any, failed_count: Any) -> dict[str, Any]:
        return {
            "checked_count": checked_count,
            "failed_count": failed_count,
            "rule_type": rule.rule_type.value,
        }

    # --- referential integrity ----------------------------------------------

    def _execute_referential_integrity(
        self,
        rule: Rule,
        source_dataset: Dataset,
        target_dataset: Dataset,
        connections: dict[int, Connection],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        errors = validate_rule_config(rule.rule_type, rule.config)
        if errors:
            raise ValueError(f"Invalid referential integrity rule configuration. {'; '.join(errors)}")
        target_values = self._collect_dataset_keys(target_dataset, rule.config["target_key"], connections)
        summary, failed_rows = self._scan_referential(rule, self._iter_dataset_rows(source_dataset, connections), target_values)
        summary["target_dataset_id"] = target_dataset.id
        return summary, failed_rows

    def _execute_referential_integrity_rule_sources(
        self,
        rule: Rule,
        source_config: dict[str, Any],
        target_source_config: dict[str, Any],
        connections: dict[int, Connection],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        errors = validate_rule_config(rule.rule_type, rule.config)
        if errors:
            raise ValueError(f"Invalid referential integrity rule configuration. {'; '.join(errors)}")
        target_values = self._collect_rule_source_keys(target_source_config, rule.config["target_key"], connections)
        return self._scan_referential(rule, self._iter_rule_source_rows(source_config, connections), target_values)

    def _scan_referential(
        self,
        rule: Rule,
        row_batches: RowBatches,
        target_values: set[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        source_key = rule.config["source_key"]
        checked_count = 0
        failed_count = 0
        failed_rows: list[dict[str, Any]] = []
        key_index: int | None = None
        for columns, rows in row_batches:
            if key_index is None:
                key_index = self._column_index(columns, source_key)
            for row in rows:
                checked_count += 1
                normalized_key = self._normalize_key(row[key_index])
                if normalized_key is None or normalized_key in target_values:
                    continue
                failed_count += 1
                if len(failed_rows) < FAILED_ROW_LIMIT:
                    failed_rows.append(dict(zip(columns, row, strict=False)))
        return self._summary(rule, checked_count, failed_count), failed_rows

    # --- row iteration ------------------------------------------------------

    def _iter_dataset_rows(self, dataset: Dataset, connections: dict[int, Connection]) -> RowBatches:
        if dataset.dataset_type in LOCAL_DATASET_TYPES:
            return self._iter_duckdb_rows(self._dataset_relation_builder(dataset, connections))
        return self._iter_oracle_rows(connections[dataset.connection_id or 0], self._oracle_dataset_sql(dataset))

    def _iter_rule_source_rows(self, source_config: dict[str, Any], connections: dict[int, Connection]) -> RowBatches:
        connection = connections[int(source_config["source_connection_id"])]
        if connection.connection_type.value == "csv":
            return self._iter_duckdb_rows(self._rule_source_relation_builder(source_config, connections))
        return self._iter_oracle_rows(connection, self.connector_service.oracle_rule_source_sql(source_config))

    def _iter_duckdb_rows(self, build_relation: RelationBuilder) -> RowBatches:
        con = duckdb.connect()
        try:
            relation = build_relation(con)
            cursor = con.execute(f"SELECT * FROM ({relation.sql_query()}) source_data")
            columns = [column[0] for column in cursor.description]
            while rows := cursor.fetchmany(FETCH_BATCH_SIZE):
                yield columns, rows
        finally:
            con.close()

    def _iter_oracle_rows(self, connection: Connection, source_sql: str) -> RowBatches:
        db_conn = self.connector_service.connect_database(connection)
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(source_sql)
                columns = [column[0] for column in cursor.description]
                while rows := cursor.fetchmany(FETCH_BATCH_SIZE):
                    yield columns, rows
        finally:
            db_conn.close()

    # --- key collection -----------------------------------------------------

    def _collect_dataset_keys(self, dataset: Dataset, key_column: str, connections: dict[int, Connection]) -> set[str]:
        if dataset.dataset_type in LOCAL_DATASET_TYPES:
            return self._collect_keys_duckdb(self._dataset_relation_builder(dataset, connections), key_column)
        return self._collect_keys_oracle(connections[dataset.connection_id or 0], self._oracle_dataset_sql(dataset), key_column)

    def _collect_rule_source_keys(self, source_config: dict[str, Any], key_column: str, connections: dict[int, Connection]) -> set[str]:
        connection = connections[int(source_config["source_connection_id"])]
        if connection.connection_type.value == "csv":
            return self._collect_keys_duckdb(self._rule_source_relation_builder(source_config, connections), key_column)
        return self._collect_keys_oracle(connection, self.connector_service.oracle_rule_source_sql(source_config), key_column)

    def _collect_keys_duckdb(self, build_relation: RelationBuilder, key_column: str) -> set[str]:
        quoted_key = self._quote_identifier(key_column)
        con = duckdb.connect()
        try:
            relation = build_relation(con)
            cursor = con.execute(
                f"SELECT DISTINCT {quoted_key} FROM ({relation.sql_query()}) target_data WHERE {quoted_key} IS NOT NULL"
            )
            return self._normalized_key_set(cursor)
        finally:
            con.close()

    def _collect_keys_oracle(self, connection: Connection, source_sql: str, key_column: str) -> set[str]:
        quoted_key = self._quote_identifier(key_column)
        db_conn = self.connector_service.connect_database(connection)
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT DISTINCT {quoted_key} FROM ({source_sql}) target_data WHERE {quoted_key} IS NOT NULL"
                )
                return self._normalized_key_set(cursor)
        finally:
            db_conn.close()

    def _normalized_key_set(self, cursor: Any) -> set[str]:
        values: set[str] = set()
        while rows := cursor.fetchmany(KEY_FETCH_BATCH_SIZE):
            for row in rows:
                normalized = self._normalize_key(row[0])
                if normalized is not None:
                    values.add(normalized)
        return values

    # --- SQL building ---------------------------------------------------------

    def _oracle_dataset_sql(self, dataset: Dataset) -> str:
        if dataset.dataset_type == DatasetType.ORACLE_SQL:
            return dataset.config["sql"]
        return f"SELECT * FROM {dataset.config['table_name']}"

    def _column_index(self, columns: list[str], requested_column: str) -> int:
        for index, column in enumerate(columns):
            if column == requested_column or column.casefold() == requested_column.casefold():
                return index
        raise ValueError(f"Field '{requested_column}' was not found in the source dataset.")

    def _normalize_key(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip()

    def _quote_identifier(self, identifier: str) -> str:
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    def _escape_literal(self, value: Any) -> str:
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    def _sql_number(self, value: Any, setting: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            try:
                value = float(str(value).strip())
            except (TypeError, ValueError):
                raise ValueError(f"The {setting} setting must be a number.") from None
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"The {setting} setting must be a finite number.")
            if value.is_integer():
                value = int(value)
        return str(value)

    def _text_expr(self, column_sql: str, dialect: str) -> str:
        if dialect == "oracle":
            return f"TO_CHAR({column_sql})"
        if dialect == "sqlserver":
            return f"CAST({column_sql} AS NVARCHAR(4000))"
        if dialect in {"db2", "sybase"}:
            return f"CAST({column_sql} AS VARCHAR(4000))"
        return f"CAST({column_sql} AS VARCHAR)"

    def _length_expr(self, text_sql: str, dialect: str) -> str:
        if dialect in {"sqlserver", "sybase"}:
            return f"LEN({text_sql})"
        return f"length({text_sql})"

    def _summary_from_clause(self, dialect: str) -> str:
        if dialect == "oracle":
            return " FROM dual"
        if dialect == "db2":
            return " FROM SYSIBM.SYSDUMMY1"
        return ""

    def _build_rule_sql(self, rule: Rule, relation_name: str, dialect: str = "duckdb") -> tuple[str, str]:
        errors = validate_rule_config(rule.rule_type, rule.config)
        if errors:
            details = "; ".join(errors)
            raise ValueError(f"Invalid {rule.rule_type.value} rule configuration. {details}")
        config = normalize_rule_config(rule.rule_type, rule.config)
        if rule.rule_type == RuleType.NOT_NULL:
            column = self._quote_identifier(config["column"])
            failed_sql = f"SELECT * FROM {relation_name} WHERE {column} IS NULL"
        elif rule.rule_type in {RuleType.UNIQUE, RuleType.DUPLICATE}:
            columns = ", ".join(self._quote_identifier(column) for column in config["columns"])
            failed_sql = (
                f"SELECT * FROM {relation_name} WHERE ({columns}) IN "
                f"(SELECT {columns} FROM {relation_name} GROUP BY {columns} HAVING COUNT(*) > 1)"
            )
        elif rule.rule_type == RuleType.ROW_COUNT:
            min_count = int(config.get("min_count", 0))
            max_count = int(config.get("max_count", 999999999999))
            failed_sql = (
                f"SELECT * FROM (SELECT COUNT(*) AS actual_count FROM {relation_name}) "
                f"WHERE actual_count < {min_count} OR actual_count > {max_count}"
            )
        elif rule.rule_type == RuleType.VALUE_RANGE:
            column = self._quote_identifier(config["column"])
            min_value = self._sql_number(config["min"], "min")
            max_value = self._sql_number(config["max"], "max")
            failed_sql = f"SELECT * FROM {relation_name} WHERE {column} < {min_value} OR {column} > {max_value}"
        elif rule.rule_type == RuleType.REGEX:
            if dialect == "sqlserver":
                raise ValueError("Regex rules are not supported on SQL Server sources.")
            if dialect == "sybase":
                raise ValueError("Regex rules are not supported on Sybase sources.")
            column = self._quote_identifier(config["column"])
            pattern = self._escape_literal(config["pattern"])
            text_column = self._text_expr(column, dialect)
            if dialect in {"oracle", "db2"}:
                failed_sql = f"SELECT * FROM {relation_name} WHERE NOT REGEXP_LIKE({text_column}, {pattern})"
            else:
                failed_sql = f"SELECT * FROM {relation_name} WHERE NOT regexp_matches({text_column}, {pattern})"
        elif rule.rule_type == RuleType.LENGTH:
            column = self._quote_identifier(config["column"])
            min_length = int(config.get("min_length", 0))
            max_length = int(config.get("max_length", 999999))
            text_column = self._text_expr(column, dialect)
            length_low = self._length_expr(text_column, dialect)
            failed_sql = (
                f"SELECT * FROM {relation_name} WHERE {length_low} < {min_length} "
                f"OR {length_low} > {max_length}"
            )
        elif rule.rule_type == RuleType.ALLOWED_VALUES:
            column = self._quote_identifier(config["column"])
            values = ", ".join(self._escape_literal(value) for value in config["values"])
            text_column = self._text_expr(column, dialect)
            failed_sql = f"SELECT * FROM {relation_name} WHERE {text_column} NOT IN ({values})"
        elif rule.rule_type == RuleType.DATE_VALIDITY:
            column = self._quote_identifier(config["column"])
            if dialect == "oracle":
                failed_sql = (
                    f"SELECT * FROM {relation_name} WHERE {column} IS NULL "
                    f"OR VALIDATE_CONVERSION({column} AS DATE) = 0"
                )
            elif dialect == "sqlserver":
                failed_sql = (
                    f"SELECT * FROM {relation_name} WHERE {column} IS NULL "
                    f"OR TRY_CONVERT(date, {self._text_expr(column, dialect)}) IS NULL"
                )
            elif dialect == "db2":
                raise ValueError("Date validity rules are not supported on DB2 sources; use a custom SQL rule instead.")
            elif dialect == "sybase":
                raise ValueError("Date validity rules are not supported on Sybase sources; use a custom SQL rule instead.")
            else:
                failed_sql = f"SELECT * FROM {relation_name} WHERE try_cast({column} AS DATE) IS NULL"
        elif rule.rule_type == RuleType.CUSTOM_SQL_FAIL_ROWS:
            failed_sql = config["sql"]
        elif rule.rule_type == RuleType.CUSTOM_SQL_THRESHOLD:
            operator = _COMPARISON_OPERATORS.get(str(config.get("operator", ">")))
            if operator is None:
                raise ValueError("The operator setting must be one of: >, >=, <, <=, ==, !=.")
            threshold = self._sql_number(config["threshold"], "threshold")
            failed_sql = f"SELECT * FROM ({config['sql']}) metric WHERE metric.value {operator} {threshold}"
        elif rule.rule_type == RuleType.REFERENTIAL_INTEGRITY:
            raise RuntimeError("Referential integrity rules require source and target selections.")
        elif rule.rule_type == RuleType.KEYED_COMPARISON:
            key_column = self._quote_identifier(config["key_column"])
            compare_columns = [self._quote_identifier(column) for column in config["compare_columns"]]
            target_relation = config["target_relation"]
            predicates = " OR ".join(
                f"COALESCE({self._text_expr(f's.{col}', dialect)}, '') <> COALESCE({self._text_expr(f't.{col}', dialect)}, '')"
                for col in compare_columns
            )
            failed_sql = (
                f"SELECT s.* FROM {relation_name} s JOIN {target_relation} t ON s.{key_column} = t.{key_column} WHERE {predicates}"
            )
        else:
            raise RuntimeError(f"Unsupported rule type: {rule.rule_type}")
        summary_sql = (
            f"SELECT (SELECT COUNT(*) FROM {relation_name}) AS checked_count, "
            f"(SELECT COUNT(*) FROM ({failed_sql}) failed) AS failed_count"
            f"{self._summary_from_clause(dialect)}"
        )
        return failed_sql, summary_sql

    # --- misc helpers ---------------------------------------------------------

    def _failed_rows_filename(self, rule: Rule) -> str:
        """Rule names are unique, but may contain characters Windows forbids in filenames."""
        safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f\s]+', "_", rule.name).strip("_.")
        return f"{safe_name or f'rule_{rule.id or 0}'}.csv"

    def _write_failed_rows(self, path: Path, rows: list[dict[str, Any]], executed_at: str) -> None:
        """Append failed rows to the rule's single CSV file, stamped with the execution datetime.

        If the rule's columns changed since earlier runs, the file is rewritten once
        with the merged header so old and new rows stay aligned.
        """
        if not rows:
            return
        enriched = [{"execution_datetime": executed_at, **row} for row in rows]
        new_fields = list(enriched[0].keys())
        existing_fields: list[str] = []
        if path.exists() and path.stat().st_size > 0:
            with path.open("r", encoding="utf-8", newline="") as handle:
                existing_fields = next(csv.reader(handle), [])
        if existing_fields == new_fields:
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=new_fields, restval="")
                writer.writerows(enriched)
            return
        merged_fields = existing_fields + [field for field in new_fields if field not in existing_fields] if existing_fields else new_fields
        old_rows: list[dict[str, Any]] = []
        if existing_fields:
            with path.open("r", encoding="utf-8", newline="") as handle:
                old_rows = list(csv.DictReader(handle))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=merged_fields, restval="", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(old_rows)
            writer.writerows(enriched)

    def _uses_embedded_source(self, rule: Rule) -> bool:
        return "source_connection_id" in rule.config

    def _extract_target_source_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_connection_id": config.get("target_connection_id"),
            "source_kind": config.get("target_kind"),
            "source_name": config.get("target_name"),
            "source_sql": config.get("target_sql"),
        }

    def _source_label(self, rule: Rule) -> str:
        if self._uses_embedded_source(rule):
            return str(rule.config.get("source_name") or rule.name)
        return f"Dataset #{rule.dataset_id}" if rule.dataset_id is not None else rule.name

    def _validate_connection_kind(self, connection: Connection, source_kind: Any, label: str) -> None:
        if source_kind == "connection":
            return
        if connection.connection_type.value == "csv" and source_kind != "csv_file":
            raise ValueError(f"The selected {label} connection is CSV and must use CSV File mode.")
        if connection.connection_type.value != "csv" and source_kind not in {"oracle_table", "oracle_sql"}:
            raise ValueError(f"The selected {label} connection is a database and must use a table or SQL source mode.")
