from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import pytest

from dqtool.models.entities import Connection, ConnectionType, Rule, RuleType
from dqtool.services.connectors import ConnectorService
from dqtool.services.execution import ExecutionService
from dqtool.services.rules import normalize_rule_config, validate_rule_config


@pytest.mark.parametrize("renamed", [False, True])
def test_csv_target_is_loaded_and_only_matching_key_differences_fail(renamed):
    connection = Connection(
        id=1, name="fixture-folder", connection_type=ConnectionType.CSV, owner_username="tester",
        config={"base_path": str(Path(__file__).parent / "fixtures")},
    )
    config = {
        "source_connection_id": 1, "source_kind": "csv_file", "source_name": "customers.csv",
        "key_column": "id", "compare_columns": ["name"],
        "target_relation": "dq_comparison_target", "comparison_target_name": "keyed_customers_target.csv",
    }
    if renamed:
        config.update({
            "comparison_target_name": "keyed_customers_renamed.csv",
            "target_key_column": "customer_number",
            "comparison_pairs": [{"source": "name", "target": "full_name"}],
        })
    connectors = ConnectorService()
    connectors.validate_keyed_comparison_fields(config, {1: connection})
    rule = Rule(id=1, name="matching customers", rule_type=RuleType.KEYED_COMPARISON,
                dataset_id=None, owner_username="tester", config=config)

    summary, rows = ExecutionService(connectors)._execute_rule_source(rule, config, {1: connection})

    assert summary["failed_count"] == 1
    assert rows[0]["name"] == "Charlie"


def test_field_validation_explains_missing_target_fields():
    connection = Connection(id=1, name="db", connection_type=ConnectionType.DB2, owner_username="tester")
    connectors = ConnectorService()
    connectors.list_rule_source_columns = Mock(side_effect=[["id", "name"], ["id"]])
    config = {"source_connection_id": 1, "key_column": "id", "compare_columns": ["name"],
              "target_relation": "CUSTOMERS_TARGET"}

    with pytest.raises(ValueError, match="Target is missing selected field"):
        connectors.validate_keyed_comparison_fields(config, {1: connection})


def test_database_comparison_does_not_double_alias_source():
    rule = Rule(id=1, name="compare", rule_type=RuleType.KEYED_COMPARISON, dataset_id=None,
                owner_username="tester", config={"key_column": "id", "compare_columns": ["name"],
                                                "target_relation": "CUSTOMERS_TARGET"})
    sql, _ = ExecutionService(ConnectorService())._build_rule_sql(rule, "(SELECT * FROM CUSTOMERS) q", "db2")

    assert ") q s" not in sql
    assert "FROM (SELECT * FROM (SELECT * FROM CUSTOMERS) q) s JOIN CUSTOMERS_TARGET t" in sql


@pytest.mark.parametrize("dialect", ["duckdb", "db2", "oracle", "sqlserver", "sybase"])
def test_sql_uses_separate_keys_and_multiple_mapped_pairs(dialect):
    rule = Rule(id=1, name="mapped", rule_type=RuleType.KEYED_COMPARISON, dataset_id=None,
                owner_username="tester", config={
                    "key_column": "id", "target_key_column": "customer_number",
                    "comparison_pairs": [{"source": "name", "target": "full_name"},
                                         {"source": "status", "target": "customer_status"}],
                    "target_relation": "CUSTOMERS_TARGET",
                })
    sql, _ = ExecutionService(ConnectorService())._build_rule_sql(rule, "dataset_view", dialect)
    assert 's."id" = t."customer_number"' in sql
    assert 's."name"' in sql and 't."full_name"' in sql
    assert 's."status"' in sql and 't."customer_status"' in sql
    assert " OR " in sql


def test_legacy_config_gains_same_name_mappings_without_losing_unknown_keys():
    config = {"key_column": "id", "compare_columns": ["name"], "future_setting": True}
    normalized = normalize_rule_config(RuleType.KEYED_COMPARISON, config)
    assert normalized["target_key_column"] == "id"
    assert normalized["comparison_pairs"] == [{"source": "name", "target": "name"}]
    assert normalized["future_setting"] is True
    assert "comparison_pairs" not in config


@pytest.mark.parametrize("pairs", [[], [{"source": "name", "target": ""}], "name", [None]])
def test_incomplete_mappings_are_rejected(pairs):
    errors = validate_rule_config(RuleType.KEYED_COMPARISON, {
        "key_column": "id", "target_key_column": "customer_number",
        "comparison_pairs": pairs, "target_relation": "TARGET",
    })
    assert errors


def test_mapped_fields_round_trip_through_rule_storage(tmp_path):
    from dqtool.services.storage import Storage

    storage = Storage(tmp_path / "project.sqlite")
    storage.initialize()
    config = {"key_column": "id", "target_key_column": "customer_number",
              "comparison_pairs": [{"source": "name", "target": "full_name"}], "target_relation": "TARGET"}
    rule = Rule(id=None, name="mapped", rule_type=RuleType.KEYED_COMPARISON, dataset_id=None,
                owner_username="tester", config=config)
    rule.id = storage.save_rule(rule)
    saved = storage.list_rules()[0]
    assert saved.config == config
    saved.config["comparison_pairs"][0]["target"] = "display_name"
    storage.save_rule(saved)
    assert storage.list_rules()[0].config["comparison_pairs"][0]["target"] == "display_name"


class ComparisonDialogTests(IsolatedAsyncioTestCase):
    async def test_create_and_edit_forms_construct_field_pairs(self):
        from dqtool.web_app import DQToolWebApp

        app = DQToolWebApp()
        app.project = SimpleNamespace()
        app._visible_connections = Mock(return_value=[
            Connection(id=1, name="db", connection_type=ConnectionType.DB2, owner_username="tester"),
        ])
        config = {"source_connection_id": 1, "source_kind": "oracle_table", "source_name": "SOURCE",
                  "key_column": "id", "compare_columns": ["name"], "target_relation": "TARGET"}
        with patch("dqtool.web_app.ui.timer"):
            app.show_rule_dialog(suggestion={"rule_type": "keyed_comparison", "config": config})
            app.show_rule_dialog(rule=Rule(id=1, name="legacy", rule_type=RuleType.KEYED_COMPARISON,
                                          dataset_id=None, owner_username="tester", config=config))

    async def test_edit_form_restores_independent_target_and_mapping(self):
        from nicegui import Client, ui

        from dqtool.web_app import DQToolWebApp

        app = DQToolWebApp()
        app.project = SimpleNamespace()
        app._visible_connections = Mock(return_value=list(csv_file_connections().values()))
        with Client(ui.page("/comparison-test"), request=None) as client, patch("dqtool.web_app.ui.timer"):
            app.show_rule_dialog(rule=cross_connection_rule())
        created = list(client.elements.values())
        selects = {element._props.get("label"): element for element in created if isinstance(element, ui.select)}
        assert selects["Source connection *"].value == "1"
        assert selects["Target connection *"].value == "2"
        assert set(selects["Target connection *"].options) == {"1", "2"}
        assert selects["Target matching key *"].value == "customer_number"
        assert selects["Source field *"].value == "name"
        assert selects["Target field *"].value == "full_name"


def cross_connection_rule():
    return Rule(id=1, name="cross-source", rule_type=RuleType.KEYED_COMPARISON, dataset_id=None,
                owner_username="tester", config={
                    "source_connection_id": 1, "source_kind": "csv_file", "source_name": "customers.csv",
                    "target_connection_id": 2, "target_kind": "csv_file", "target_name": "keyed_customers_renamed.csv",
                    "key_column": "id", "target_key_column": "customer_number",
                    "comparison_pairs": [{"source": "name", "target": "full_name"}],
                })


def csv_file_connections():
    fixtures = Path(__file__).parent / "fixtures"
    return {
        1: Connection(id=1, name="source-file", connection_type=ConnectionType.CSV, owner_username="tester",
                      config={"base_path": str(fixtures / "customers.csv")}),
        2: Connection(id=2, name="target-file", connection_type=ConnectionType.CSV, owner_username="tester",
                      config={"base_path": str(fixtures / "keyed_customers_renamed.csv")}),
    }


def test_separate_single_file_csv_connections_compare_correctly():
    rule = cross_connection_rule()
    connections = csv_file_connections()
    connectors = ConnectorService()
    connectors.validate_keyed_comparison_fields(rule.config, connections)

    summary, rows = ExecutionService(connectors)._execute_rule(rule, {}, connections)

    assert summary["checked_count"] == 3
    assert summary["failed_count"] == 1
    assert rows == [{"id": 2, "name": "Charlie"}]


@pytest.mark.parametrize("direction", ["csv_to_database", "database_to_csv", "database_to_database"])
def test_comparisons_across_database_and_csv_connections(direction):
    rule = cross_connection_rule()
    connections = csv_file_connections()
    service = ExecutionService(ConnectorService())
    if direction != "csv_to_database":
        connections[1].connection_type = ConnectionType.ORACLE
        rule.config.update(source_kind="oracle_table", source_name="SOURCE_CUSTOMERS")
    if direction != "database_to_csv":
        connections[2].connection_type = ConnectionType.DB2
        rule.config.update(target_kind="oracle_sql", target_sql="SELECT customer_number, full_name FROM TARGET_CUSTOMERS")

    def database_rows(connection, sql):
        if connection.id == 1:
            assert sql == "SELECT * FROM SOURCE_CUSTOMERS"
            return iter([(["id", "name"], [(1, "Alice"), (2, "Bob"), (2, "Charlie")])])
        assert sql == "SELECT customer_number, full_name FROM TARGET_CUSTOMERS"
        return iter([(["customer_number", "full_name"], [(2, "Bob"), (3, "Dana")])])

    service._iter_database_rows = Mock(side_effect=database_rows)
    summary, rows = service._execute_rule(rule, {}, connections)
    assert summary["failed_count"] == 1
    assert rows == [{"id": 2, "name": "Charlie"}]


def test_same_database_target_query_is_joined_without_local_loading():
    rule = cross_connection_rule()
    rule.config.update(source_kind="oracle_table", source_name="SOURCE_CUSTOMERS", target_connection_id=1,
                       target_kind="oracle_sql", target_sql="SELECT customer_number, full_name FROM TARGET_CUSTOMERS")
    connections = {1: Connection(id=1, name="db", connection_type=ConnectionType.DB2, owner_username="tester")}
    service = ExecutionService(ConnectorService())
    service._run_database_rule = Mock(return_value=({}, []))
    service._execute_rule(rule, {}, connections)
    sql_rule, connection, source_sql = service._run_database_rule.call_args.args
    assert connection.id == 1
    assert source_sql == "SELECT * FROM SOURCE_CUSTOMERS"
    assert sql_rule.config["target_relation"] == "(SELECT customer_number, full_name FROM TARGET_CUSTOMERS)"
    assert "target_relation" not in rule.config
    queries = service.preview_rule_sql(rule, connections)
    assert 's."id" = t."customer_number"' in queries[0][1]
    assert "SELECT customer_number, full_name FROM TARGET_CUSTOMERS" in queries[0][1]


def test_independent_target_field_validation_uses_target_connection():
    rule = cross_connection_rule()
    connectors = ConnectorService()
    connectors.list_rule_source_columns = Mock(side_effect=[["id", "name"], ["customer_number", "full_name"]])
    connectors.validate_keyed_comparison_fields(rule.config, csv_file_connections())
    target_config = connectors.list_rule_source_columns.call_args_list[1].args[0]
    assert target_config["source_connection_id"] == 2
    assert target_config["source_name"] == "keyed_customers_renamed.csv"


def test_cross_source_scheduler_run_retains_runtime_and_failed_evidence(tmp_path):
    rule = cross_connection_rule()
    run = ExecutionService(ConnectorService()).run_rules(
        [rule], {}, csv_file_connections(), tmp_path, "scheduler",
    )[0]
    assert run.status == "failed"
    assert run.executed_by == "scheduler"
    assert run.runtime_ms is not None
    assert Path(run.failed_rows_path).exists()


def test_missing_target_connection_is_rejected_before_reading_sources():
    rule = cross_connection_rule()
    service = ExecutionService(ConnectorService())
    service._iter_rule_source_rows = Mock()
    with pytest.raises(ValueError, match="target connection no longer exists or is not accessible"):
        service._execute_rule(rule, {}, {1: csv_file_connections()[1]})
    service._iter_rule_source_rows.assert_not_called()


def test_target_reference_and_mappings_are_validated_without_legacy_relation():
    rule = cross_connection_rule()
    assert validate_rule_config(rule.rule_type, rule.config, require_source=True) == []
    rule.config["target_kind"] = "oracle_sql"
    assert "Target SQL is required for a custom SQL source." in validate_rule_config(rule.rule_type, rule.config, require_source=True)


def test_cross_source_preview_shows_read_queries_and_scheduling_tracks_both_connections():
    rule = cross_connection_rule()
    service = ExecutionService(ConnectorService())
    queries = service.preview_rule_sql(rule, csv_file_connections())
    assert len(queries) == 2
    assert "read_csv" in queries[0][1] and "customers.csv" in queries[0][1]
    assert "read_csv" in queries[1][1] and "keyed_customers_renamed.csv" in queries[1][1]
    assert service._rule_connection_ids(rule, {}) == {1, 2}


def test_cross_source_nulls_missing_keys_duplicates_and_preview_limit():
    rule = cross_connection_rule()
    service = ExecutionService(ConnectorService())
    source_batches = iter([(["id", "name"], [(None, "skip"), (9, "missing"), (1, None), (2, "wrong")])])
    target_batches = iter([(["customer_number", "full_name"], [(None, "skip"), (1, ""), *[(2, "right")] * 501])])
    summary, rows = service._scan_keyed_comparison(rule, source_batches, target_batches)
    assert summary["checked_count"] == 4
    assert summary["failed_count"] == 501
    assert len(rows) == 500
    assert all(row["id"] == 2 for row in rows)
