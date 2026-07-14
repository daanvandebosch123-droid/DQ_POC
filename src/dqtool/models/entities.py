from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    """Role inside a project: Admins manage members and all items, Users manage their own items."""

    ADMIN = "Admin"
    USER = "User"


class WorkspaceRole(StrEnum):
    """Workspace-wide role: the Workspace Admin manages accounts, projects, and access."""

    WORKSPACE_ADMIN = "Workspace Admin"
    MEMBER = "Member"


class ConnectionType(StrEnum):
    ORACLE = "oracle"
    CSV = "csv"
    SQLSERVER = "sqlserver"
    DB2 = "db2"


class DatasetType(StrEnum):
    ORACLE_TABLE = "oracle_table"
    ORACLE_SQL = "oracle_sql"
    CSV_FILE = "csv_file"
    CSV_FOLDER_FILE = "csv_folder_file"
    APP_JOIN = "app_join"


class RuleType(StrEnum):
    NOT_NULL = "not_null"
    UNIQUE = "unique"
    DUPLICATE = "duplicate"
    ROW_COUNT = "row_count"
    VALUE_RANGE = "value_range"
    REGEX = "regex"
    LENGTH = "length"
    ALLOWED_VALUES = "allowed_values"
    DATE_VALIDITY = "date_validity"
    CUSTOM_SQL_FAIL_ROWS = "custom_sql_fail_rows"
    CUSTOM_SQL_THRESHOLD = "custom_sql_threshold"
    REFERENTIAL_INTEGRITY = "referential_integrity"
    KEYED_COMPARISON = "keyed_comparison"


@dataclass(slots=True)
class User:
    id: int | None
    username: str
    role: WorkspaceRole
    created_at: str | None = None


@dataclass(slots=True)
class Project:
    id: int | None
    name: str
    folder_name: str
    created_by: str
    created_at: str | None = None


@dataclass(slots=True)
class Connection:
    id: int | None
    name: str
    connection_type: ConnectionType
    owner_username: str
    visibility: str = "private"
    allowed_users: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    updated_at: str | None = None


@dataclass(slots=True)
class Dataset:
    id: int | None
    name: str
    dataset_type: DatasetType
    connection_id: int | None
    owner_username: str
    visibility: str = "private"
    allowed_users: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    updated_at: str | None = None


@dataclass(slots=True)
class Rule:
    id: int | None
    name: str
    rule_type: RuleType
    dataset_id: int | None
    owner_username: str
    visibility: str = "private"
    allowed_users: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    updated_at: str | None = None


@dataclass(slots=True)
class RuleGroup:
    id: int | None
    name: str
    owner_username: str
    visibility: str = "private"
    allowed_users: list[str] = field(default_factory=list)
    rule_ids: list[int] = field(default_factory=list)
    child_group_ids: list[int] = field(default_factory=list)
    updated_at: str | None = None


@dataclass(slots=True)
class RuleRun:
    id: int | None
    rule_id: int
    dataset_id: int
    status: str
    executed_by: str
    started_at: str
    finished_at: str | None = None
    summary_json: dict[str, Any] = field(default_factory=dict)
    failed_rows_path: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
