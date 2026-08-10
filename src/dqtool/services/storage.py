from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dqtool.models.entities import (
    Connection,
    ConnectionType,
    Dataset,
    DatasetType,
    Role,
    Rule,
    RuleGroup,
    RuleRun,
    RuleType,
    Schedule,
    ScheduleCadence,
    ScheduleTargetKind,
    User,
    utc_now,
)
from dqtool.services.rules import would_create_cycle


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, roll back on error, always close."""
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._session() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    connection_type TEXT NOT NULL,
                    owner_username TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    allowed_users_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS datasets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    dataset_type TEXT NOT NULL,
                    connection_id INTEGER,
                    owner_username TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    allowed_users_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(connection_id) REFERENCES connections(id)
                );

                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    rule_type TEXT NOT NULL,
                    dataset_id INTEGER,
                    owner_username TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL,
                    allowed_users_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(dataset_id) REFERENCES datasets(id)
                );

                CREATE TABLE IF NOT EXISTS rule_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL,
                    dataset_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    executed_by TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    summary_json TEXT NOT NULL,
                    failed_rows_path TEXT,
                    schedule_id INTEGER,
                    runtime_ms INTEGER,
                    FOREIGN KEY(rule_id) REFERENCES rules(id),
                    FOREIGN KEY(dataset_id) REFERENCES datasets(id),
                    FOREIGN KEY(schedule_id) REFERENCES schedules(id)
                );

                CREATE TABLE IF NOT EXISTS rule_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    owner_username TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    allowed_users_json TEXT NOT NULL,
                    rule_ids_json TEXT NOT NULL,
                    child_group_ids_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL,
                    profiled_at TEXT NOT NULL,
                    profile_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    target_kind TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    cadence TEXT NOT NULL,
                    interval_hours INTEGER NOT NULL DEFAULT 1,
                    time_of_day TEXT NOT NULL DEFAULT '00:00',
                    weekday INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    owner_username TEXT NOT NULL,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    last_status TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Projects created before folders were removed still carry the column.
            for table in ("connections", "datasets", "rules"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if "folder" in columns:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN folder")
            rule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rules)").fetchall()}
            if "description" not in rule_columns:
                conn.execute("ALTER TABLE rules ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rule_runs)").fetchall()}
            if "schedule_id" not in run_columns:
                conn.execute("ALTER TABLE rule_runs ADD COLUMN schedule_id INTEGER")
            if "runtime_ms" not in run_columns:
                conn.execute("ALTER TABLE rule_runs ADD COLUMN runtime_ms INTEGER")
            # Projects created before nested groups still lack this column.
            group_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rule_groups)").fetchall()}
            if "child_group_ids_json" not in group_columns:
                conn.execute("ALTER TABLE rule_groups ADD COLUMN child_group_ids_json TEXT NOT NULL DEFAULT '[]'")

    def ensure_initial_admin(self, username: str) -> None:
        with self._session() as conn:
            existing = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
            if existing == 0:
                conn.execute(
                    "INSERT INTO users(username, role, created_at) VALUES (?, ?, ?)",
                    (username, Role.ADMIN.value, utc_now()),
                )

    def list_users(self) -> list[User]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [User(id=row["id"], username=row["username"], role=Role(row["role"]), created_at=row["created_at"]) for row in rows]

    def upsert_user(self, username: str, role: Role) -> None:
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO users(username, role, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET role = excluded.role
                """,
                (username, role.value, utc_now()),
            )

    def get_user(self, username: str) -> User | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return None
        return User(id=row["id"], username=row["username"], role=Role(row["role"]), created_at=row["created_at"])

    def list_connections(self) -> list[Connection]:
        return self._list_entity("connections", self._row_to_connection)

    def save_connection(self, connection: Connection) -> int:
        name = self._dedupe_name("connections", connection.name, connection.id)
        payload = (
            name,
            connection.connection_type.value,
            connection.owner_username,
            connection.visibility,
            json.dumps(connection.allowed_users),
            json.dumps(connection.config),
            json.dumps(connection.tags),
            utc_now(),
        )
        with self._session() as conn:
            if connection.id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO connections(
                        name, connection_type, owner_username, visibility, allowed_users_json,
                        config_json, tags_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                return int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE connections
                SET name=?, connection_type=?, owner_username=?, visibility=?, allowed_users_json=?,
                    config_json=?, tags_json=?, updated_at=?
                WHERE id=?
                """,
                payload + (connection.id,),
            )
            return connection.id

    def list_datasets(self) -> list[Dataset]:
        return self._list_entity("datasets", self._row_to_dataset)

    def save_dataset(self, dataset: Dataset) -> int:
        name = self._dedupe_name("datasets", dataset.name, dataset.id)
        payload = (
            name,
            dataset.dataset_type.value,
            dataset.connection_id,
            dataset.owner_username,
            dataset.visibility,
            json.dumps(dataset.allowed_users),
            json.dumps(dataset.config),
            json.dumps(dataset.tags),
            utc_now(),
        )
        with self._session() as conn:
            if dataset.id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO datasets(
                        name, dataset_type, connection_id, owner_username, visibility, allowed_users_json,
                        config_json, tags_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                return int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE datasets
                SET name=?, dataset_type=?, connection_id=?, owner_username=?, visibility=?, allowed_users_json=?,
                    config_json=?, tags_json=?, updated_at=?
                WHERE id=?
                """,
                payload + (dataset.id,),
            )
            return dataset.id

    def list_rules(self) -> list[Rule]:
        return self._list_entity("rules", self._row_to_rule)

    def save_rule(self, rule: Rule) -> int:
        name = self._dedupe_name("rules", rule.name, rule.id)
        payload = (
            name,
            rule.rule_type.value,
            rule.dataset_id,
            rule.owner_username,
            rule.description,
            rule.visibility,
            json.dumps(rule.allowed_users),
            json.dumps(rule.config),
            json.dumps(rule.tags),
            utc_now(),
        )
        with self._session() as conn:
            if rule.id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO rules(
                        name, rule_type, dataset_id, owner_username, description, visibility, allowed_users_json,
                        config_json, tags_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                return int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE rules
                SET name=?, rule_type=?, dataset_id=?, owner_username=?, description=?, visibility=?, allowed_users_json=?,
                    config_json=?, tags_json=?, updated_at=?
                WHERE id=?
                """,
                payload + (rule.id,),
            )
            return rule.id

    def delete_connection(self, connection_id: int) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))

    def delete_rule(self, rule_id: int) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            for row in conn.execute("SELECT id, rule_ids_json FROM rule_groups").fetchall():
                rule_ids = json.loads(row["rule_ids_json"])
                if rule_id in rule_ids:
                    conn.execute(
                        "UPDATE rule_groups SET rule_ids_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps([item for item in rule_ids if item != rule_id]), utc_now(), row["id"]),
                    )

    def list_rule_groups(self) -> list[RuleGroup]:
        return self._list_entity("rule_groups", self._row_to_rule_group)

    def save_rule_group(self, group: RuleGroup) -> int:
        groups_by_id = {item.id: item for item in self.list_rule_groups() if item.id is not None}
        if group.child_group_ids:
            if group.id is not None and group.id in group.child_group_ids:
                raise ValueError("A group cannot contain itself.")
            if would_create_cycle(group.id, group.child_group_ids, groups_by_id):
                raise ValueError("That subgroup selection would create a nesting cycle.")
        name = self._dedupe_name("rule_groups", group.name, group.id)
        payload = (
            name,
            group.owner_username,
            group.visibility,
            json.dumps(group.allowed_users),
            json.dumps(group.rule_ids),
            json.dumps(group.child_group_ids),
            utc_now(),
        )
        with self._session() as conn:
            if group.id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO rule_groups(
                        name, owner_username, visibility, allowed_users_json, rule_ids_json,
                        child_group_ids_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                return int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE rule_groups
                SET name=?, owner_username=?, visibility=?, allowed_users_json=?, rule_ids_json=?,
                    child_group_ids_json=?, updated_at=?
                WHERE id=?
                """,
                payload + (group.id,),
            )
            return group.id

    def move_rule_to_group(self, rule_id: int, target_group_id: int, source_group_ids: list[int]) -> None:
        """Make a rule a direct member of one group and remove selected old memberships.

        ``source_group_ids`` is supplied by the UI after its permission checks, so a
        project user can move rules between the groups they manage without changing
        other owners' groups.  All changes are committed together.
        """
        with self._session() as conn:
            if conn.execute("SELECT 1 FROM rules WHERE id = ?", (rule_id,)).fetchone() is None:
                raise ValueError("The rule no longer exists.")
            target = conn.execute("SELECT rule_ids_json FROM rule_groups WHERE id = ?", (target_group_id,)).fetchone()
            if target is None:
                raise ValueError("The target group no longer exists.")

            now = utc_now()
            target_rule_ids = json.loads(target["rule_ids_json"])
            if rule_id not in target_rule_ids:
                target_rule_ids.append(rule_id)
                conn.execute(
                    "UPDATE rule_groups SET rule_ids_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(target_rule_ids), now, target_group_id),
                )

            for source_group_id in set(source_group_ids) - {target_group_id}:
                row = conn.execute("SELECT rule_ids_json FROM rule_groups WHERE id = ?", (source_group_id,)).fetchone()
                if row is None:
                    continue
                rule_ids = json.loads(row["rule_ids_json"])
                if rule_id in rule_ids:
                    conn.execute(
                        "UPDATE rule_groups SET rule_ids_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps([item for item in rule_ids if item != rule_id]), now, source_group_id),
                    )

    def delete_rule_group(self, group_id: int) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM rule_groups WHERE id = ?", (group_id,))
            for row in conn.execute("SELECT id, child_group_ids_json FROM rule_groups").fetchall():
                child_group_ids = json.loads(row["child_group_ids_json"])
                if group_id in child_group_ids:
                    conn.execute(
                        "UPDATE rule_groups SET child_group_ids_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps([item for item in child_group_ids if item != group_id]), utc_now(), row["id"]),
                    )

    def delete_rule_run(self, run_id: int) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM rule_runs WHERE id = ?", (run_id,))

    def list_rule_runs(self, limit: int = 100) -> list[RuleRun]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM rule_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            RuleRun(
                id=row["id"],
                rule_id=row["rule_id"],
                dataset_id=row["dataset_id"],
                status=row["status"],
                executed_by=row["executed_by"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                summary_json=json.loads(row["summary_json"]),
                failed_rows_path=row["failed_rows_path"],
                schedule_id=row["schedule_id"],
                runtime_ms=row["runtime_ms"],
            )
            for row in rows
        ]

    def save_rule_run(self, run: RuleRun) -> int:
        with self._session() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rule_runs(
                    rule_id, dataset_id, status, executed_by, started_at, finished_at, summary_json, failed_rows_path, schedule_id,
                    runtime_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.rule_id,
                    run.dataset_id,
                    run.status,
                    run.executed_by,
                    run.started_at,
                    run.finished_at,
                    json.dumps(run.summary_json),
                    run.failed_rows_path,
                    run.schedule_id,
                    run.runtime_ms,
                ),
            )
            return int(cursor.lastrowid)

    def list_schedules(self) -> list[Schedule]:
        return self._list_entity("schedules", self._row_to_schedule)

    def get_schedule(self, schedule_id: int) -> Schedule | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return self._row_to_schedule(row) if row else None

    def save_schedule(self, schedule: Schedule) -> int:
        name = self._dedupe_name("schedules", schedule.name, schedule.id)
        payload = (
            name,
            schedule.target_kind.value,
            schedule.target_id,
            schedule.cadence.value,
            int(schedule.interval_hours),
            schedule.time_of_day,
            int(schedule.weekday),
            1 if schedule.enabled else 0,
            schedule.owner_username,
            schedule.last_run_at,
            schedule.next_run_at,
            schedule.last_status,
            utc_now(),
        )
        with self._session() as conn:
            if schedule.id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO schedules(
                        name, target_kind, target_id, cadence, interval_hours, time_of_day, weekday,
                        enabled, owner_username, last_run_at, next_run_at, last_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                return int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE schedules
                SET name=?, target_kind=?, target_id=?, cadence=?, interval_hours=?, time_of_day=?, weekday=?,
                    enabled=?, owner_username=?, last_run_at=?, next_run_at=?, last_status=?, updated_at=?
                WHERE id=?
                """,
                payload + (schedule.id,),
            )
            return schedule.id

    def delete_schedule(self, schedule_id: int) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

    def list_due_schedules(self, now_iso: str) -> list[Schedule]:
        """Enabled schedules whose next_run_at has arrived (or has no next_run_at set yet)."""
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM schedules
                WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY id
                """,
                (now_iso,),
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    def record_schedule_run(self, schedule_id: int, last_run_at: str, next_run_at: str, last_status: str) -> None:
        with self._session() as conn:
            conn.execute(
                """
                UPDATE schedules
                SET last_run_at = ?, next_run_at = ?, last_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (last_run_at, next_run_at, last_status, utc_now(), schedule_id),
            )

    def save_source_profile(self, source_key: str, profile: dict[str, Any]) -> int:
        with self._session() as conn:
            cursor = conn.execute(
                "INSERT INTO source_profiles(source_key, profiled_at, profile_json) VALUES (?, ?, ?)",
                (source_key, profile.get("profiled_at") or utc_now(), json.dumps(profile)),
            )
            return int(cursor.lastrowid)

    def list_source_profiles(self, source_key: str, limit: int = 50) -> list[dict[str, Any]]:
        """Profile snapshots for a source, oldest first."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT profile_json FROM source_profiles WHERE source_key = ? ORDER BY id DESC LIMIT ?",
                (source_key, limit),
            ).fetchall()
        return [json.loads(row["profile_json"]) for row in reversed(rows)]

    def latest_source_profile(self, source_key: str) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT profile_json FROM source_profiles WHERE source_key = ? ORDER BY id DESC LIMIT 1",
                (source_key,),
            ).fetchone()
        return json.loads(row["profile_json"]) if row else None

    def _list_entity(self, table: str, mapper) -> list[Any]:
        with self._session() as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY name").fetchall()
        return [mapper(row) for row in rows]

    def _row_to_connection(self, row: sqlite3.Row) -> Connection:
        return Connection(
            id=row["id"],
            name=row["name"],
            connection_type=ConnectionType(row["connection_type"]),
            owner_username=row["owner_username"],
            visibility=row["visibility"],
            allowed_users=json.loads(row["allowed_users_json"]),
            config=json.loads(row["config_json"]),
            tags=json.loads(row["tags_json"]),
            updated_at=row["updated_at"],
        )

    def _row_to_dataset(self, row: sqlite3.Row) -> Dataset:
        return Dataset(
            id=row["id"],
            name=row["name"],
            dataset_type=DatasetType(row["dataset_type"]),
            connection_id=row["connection_id"],
            owner_username=row["owner_username"],
            visibility=row["visibility"],
            allowed_users=json.loads(row["allowed_users_json"]),
            config=json.loads(row["config_json"]),
            tags=json.loads(row["tags_json"]),
            updated_at=row["updated_at"],
        )

    def _row_to_rule(self, row: sqlite3.Row) -> Rule:
        return Rule(
            id=row["id"],
            name=row["name"],
            rule_type=RuleType(row["rule_type"]),
            dataset_id=row["dataset_id"],
            owner_username=row["owner_username"],
            description=row["description"],
            visibility=row["visibility"],
            allowed_users=json.loads(row["allowed_users_json"]),
            config=json.loads(row["config_json"]),
            tags=json.loads(row["tags_json"]),
            updated_at=row["updated_at"],
        )

    def _row_to_rule_group(self, row: sqlite3.Row) -> RuleGroup:
        return RuleGroup(
            id=row["id"],
            name=row["name"],
            owner_username=row["owner_username"],
            visibility=row["visibility"],
            allowed_users=json.loads(row["allowed_users_json"]),
            rule_ids=json.loads(row["rule_ids_json"]),
            child_group_ids=json.loads(row["child_group_ids_json"]),
            updated_at=row["updated_at"],
        )

    def _row_to_schedule(self, row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=row["id"],
            name=row["name"],
            target_kind=ScheduleTargetKind(row["target_kind"]),
            target_id=row["target_id"],
            cadence=ScheduleCadence(row["cadence"]),
            interval_hours=row["interval_hours"],
            time_of_day=row["time_of_day"],
            weekday=row["weekday"],
            enabled=bool(row["enabled"]),
            owner_username=row["owner_username"],
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
            last_status=row["last_status"],
            updated_at=row["updated_at"],
        )

    def _dedupe_name(self, table: str, requested_name: str, existing_id: int | None) -> str:
        candidate = requested_name
        index = 2
        with self._session() as conn:
            while True:
                if existing_id is None:
                    row = conn.execute(
                        f"SELECT id FROM {table} WHERE name = ?",
                        (candidate,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"SELECT id FROM {table} WHERE name = ? AND id != ?",
                        (candidate, existing_id),
                    ).fetchone()
                if row is None:
                    return candidate
                candidate = f"{requested_name} ({index})"
                index += 1
