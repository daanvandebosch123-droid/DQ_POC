from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dqtool.models.entities import Connection, ConnectionType, Rule, RuleGroup, RuleType
from dqtool.services.connectors import ConnectorService
from dqtool.services.execution import ExecutionService
from dqtool.services.rules import resolve_group_rules, would_create_cycle
from dqtool.services.storage import Storage


class RuleGroupStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = Storage(Path(tempfile.mkdtemp()) / "test.sqlite")
        self.storage.initialize()
        self.rule_a = self.storage.save_rule(
            Rule(id=None, name="a", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester")
        )
        self.rule_b = self.storage.save_rule(
            Rule(id=None, name="b", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester")
        )

    def test_group_crud(self) -> None:
        group_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="daily checks", owner_username="tester", rule_ids=[self.rule_a, self.rule_b])
        )
        group = self.storage.list_rule_groups()[0]
        self.assertEqual("daily checks", group.name)
        self.assertEqual([self.rule_a, self.rule_b], group.rule_ids)

        group.rule_ids = [self.rule_b]
        self.assertEqual(group_id, self.storage.save_rule_group(group))
        self.assertEqual([self.rule_b], self.storage.list_rule_groups()[0].rule_ids)

        self.storage.delete_rule_group(group_id)
        self.assertEqual([], self.storage.list_rule_groups())

    def test_rule_description_round_trips_through_storage(self) -> None:
        rule_id = self.storage.save_rule(
            Rule(
                id=None,
                name="documented rule",
                rule_type=RuleType.NOT_NULL,
                dataset_id=None,
                owner_username="tester",
                description="Customer ID is mandatory for downstream matching.",
            )
        )

        rule = next(item for item in self.storage.list_rules() if item.id == rule_id)
        self.assertEqual("Customer ID is mandatory for downstream matching.", rule.description)

    def test_deleting_a_rule_removes_it_from_groups(self) -> None:
        self.storage.save_rule_group(
            RuleGroup(id=None, name="daily checks", owner_username="tester", rule_ids=[self.rule_a, self.rule_b])
        )
        self.storage.delete_rule(self.rule_a)
        self.assertEqual([self.rule_b], self.storage.list_rule_groups()[0].rule_ids)

    def test_move_rule_to_group_replaces_selected_direct_memberships(self) -> None:
        source_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="source", owner_username="tester", rule_ids=[self.rule_a, self.rule_b])
        )
        destination_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="destination", owner_username="tester", rule_ids=[self.rule_b])
        )

        self.storage.move_rule_to_group(self.rule_a, destination_id, [source_id])

        groups = {group.id: group for group in self.storage.list_rule_groups()}
        self.assertEqual([self.rule_b], groups[source_id].rule_ids)
        self.assertEqual([self.rule_b, self.rule_a], groups[destination_id].rule_ids)

    def test_nested_group_persists_child_group_ids(self) -> None:
        parent_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="parent", owner_username="tester", rule_ids=[self.rule_a])
        )
        child_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="child", owner_username="tester", rule_ids=[self.rule_b])
        )
        parent = next(g for g in self.storage.list_rule_groups() if g.id == parent_id)
        parent.child_group_ids = [child_id]
        self.storage.save_rule_group(parent)

        reloaded = next(g for g in self.storage.list_rule_groups() if g.id == parent_id)
        self.assertEqual([child_id], reloaded.child_group_ids)

    def test_deleting_a_group_removes_it_from_parents(self) -> None:
        child_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="child", owner_username="tester", rule_ids=[self.rule_b])
        )
        parent_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="parent", owner_username="tester", rule_ids=[self.rule_a], child_group_ids=[child_id])
        )
        self.storage.delete_rule_group(child_id)
        parent = next(g for g in self.storage.list_rule_groups() if g.id == parent_id)
        self.assertEqual([], parent.child_group_ids)

    def test_saving_a_direct_self_reference_is_rejected(self) -> None:
        group_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="loop", owner_username="tester", rule_ids=[self.rule_a])
        )
        group = next(g for g in self.storage.list_rule_groups() if g.id == group_id)
        group.child_group_ids = [group_id]
        with self.assertRaises(ValueError):
            self.storage.save_rule_group(group)

    def test_saving_a_transitive_cycle_is_rejected(self) -> None:
        # a -> b -> c, then trying to save c -> a should be rejected.
        a_id = self.storage.save_rule_group(RuleGroup(id=None, name="a", owner_username="tester", rule_ids=[self.rule_a]))
        b_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="b", owner_username="tester", rule_ids=[self.rule_b], child_group_ids=[])
        )
        c_id = self.storage.save_rule_group(
            RuleGroup(id=None, name="c", owner_username="tester", rule_ids=[self.rule_a], child_group_ids=[])
        )
        a = next(g for g in self.storage.list_rule_groups() if g.id == a_id)
        a.child_group_ids = [b_id]
        self.storage.save_rule_group(a)
        b = next(g for g in self.storage.list_rule_groups() if g.id == b_id)
        b.child_group_ids = [c_id]
        self.storage.save_rule_group(b)

        c = next(g for g in self.storage.list_rule_groups() if g.id == c_id)
        c.child_group_ids = [a_id]
        with self.assertRaises(ValueError):
            self.storage.save_rule_group(c)


class RuleGroupResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule_a = Rule(id=1, name="a", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester")
        self.rule_b = Rule(id=2, name="b", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester")
        self.rules_by_id = {1: self.rule_a, 2: self.rule_b}

    def test_flattens_nested_groups_and_dedupes_shared_rules(self) -> None:
        child = RuleGroup(id=10, name="child", owner_username="tester", rule_ids=[2])
        parent = RuleGroup(id=11, name="parent", owner_username="tester", rule_ids=[1, 2], child_group_ids=[10])
        groups_by_id = {10: child, 11: parent}

        rules, missing_rules, missing_groups = resolve_group_rules(parent, groups_by_id, self.rules_by_id)

        # rule 2 is a direct member AND reachable through the child group; it must appear once.
        self.assertEqual([self.rule_a, self.rule_b], rules)
        self.assertEqual(0, missing_rules)
        self.assertEqual(0, missing_groups)

    def test_counts_missing_rules_and_subgroups(self) -> None:
        parent = RuleGroup(id=11, name="parent", owner_username="tester", rule_ids=[1, 999], child_group_ids=[404])
        rules, missing_rules, missing_groups = resolve_group_rules(parent, {}, self.rules_by_id)

        self.assertEqual([self.rule_a], rules)
        self.assertEqual(1, missing_rules)
        self.assertEqual(1, missing_groups)

    def test_would_create_cycle_detects_transitive_loop(self) -> None:
        a = RuleGroup(id=1, name="a", owner_username="tester", child_group_ids=[])
        b = RuleGroup(id=2, name="b", owner_username="tester", child_group_ids=[])
        groups_by_id = {1: a, 2: b}

        # a -> b is fine.
        self.assertFalse(would_create_cycle(1, [2], groups_by_id))

        # Once b -> a exists (a is b's ancestor), nesting b under a would close the loop.
        b.child_group_ids = [1]
        self.assertTrue(would_create_cycle(1, [2], groups_by_id))


class RuleGroupExecutionTests(unittest.TestCase):
    def test_grouped_rules_run_together_and_produce_one_run_each(self) -> None:
        fixtures = Path(__file__).parent / "fixtures"
        results_dir = Path(tempfile.mkdtemp())
        connection = Connection(
            id=11,
            name="fixture-csv",
            connection_type=ConnectionType.CSV,
            owner_username="tester",
            config={"base_path": str(fixtures)},
        )
        source = {"source_connection_id": 11, "source_kind": "csv_file", "source_name": "customers.csv", "source_sql": ""}
        rules = [
            Rule(id=1, name="unique ids", rule_type=RuleType.UNIQUE, dataset_id=None, owner_username="tester",
                 config={**source, "columns": ["id"]}),
            Rule(id=2, name="name not null", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester",
                 config={**source, "column": "name"}),
        ]

        runs = ExecutionService(ConnectorService()).run_rules(rules, {}, {11: connection}, results_dir, "tester")

        self.assertEqual([1, 2], [run.rule_id for run in runs])
        self.assertEqual(["failed", "passed"], [run.status for run in runs])


if __name__ == "__main__":
    unittest.main()
