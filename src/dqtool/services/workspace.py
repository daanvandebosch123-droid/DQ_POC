from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dqtool.models.entities import Project, Role, User, WorkspaceRole, utc_now

WORKSPACE_DB = "dqtool_workspace.sqlite"
PROJECT_DB = "dqtool_project.sqlite"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"


def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-SHA256 hash stored as 'salt$digest' (hex). POC-grade, swappable later."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


class WorkspaceStorage:
    """Workspace-level registry: shared users, projects, and project memberships."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
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
                    password_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    folder_name TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_members (
                    project_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'User',
                    PRIMARY KEY (project_id, username),
                    FOREIGN KEY (project_id) REFERENCES projects(id)
                );
                """
            )
            # Workspaces created before per-project roles: add the member role column.
            member_columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_members)").fetchall()}
            if "role" not in member_columns:
                conn.execute("ALTER TABLE project_members ADD COLUMN role TEXT NOT NULL DEFAULT 'User'")
            # Workspaces created before workspace roles: map the old global roles onto the new ones.
            conn.execute("UPDATE users SET role = ? WHERE role = 'Admin'", (WorkspaceRole.WORKSPACE_ADMIN.value,))
            conn.execute("UPDATE users SET role = ? WHERE role = 'User'", (WorkspaceRole.MEMBER.value,))

    # -- users -------------------------------------------------------------

    def seed_default_admin(self) -> None:
        """A fresh workspace gets an 'admin'/'admin' account; change the password after first login."""
        with self._session() as conn:
            count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
            if count == 0:
                conn.execute(
                    "INSERT INTO users(username, role, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (
                        DEFAULT_ADMIN_USERNAME,
                        WorkspaceRole.WORKSPACE_ADMIN.value,
                        hash_password(DEFAULT_ADMIN_PASSWORD),
                        utc_now(),
                    ),
                )

    def list_users(self) -> list[User]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [self._row_to_user(row) for row in rows]

    def get_user(self, username: str) -> User | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return self._row_to_user(row) if row else None

    def is_workspace_admin(self, username: str) -> bool:
        user = self.get_user(username)
        return user is not None and user.role == WorkspaceRole.WORKSPACE_ADMIN

    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(id=row["id"], username=row["username"], role=WorkspaceRole(row["role"]), created_at=row["created_at"])

    def upsert_user(self, username: str, role: WorkspaceRole, password: str | None = None) -> None:
        """Create or update an account. Password is required for new accounts; None keeps the current one."""
        with self._session() as conn:
            existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if existing is None:
                if not password:
                    raise ValueError("A password is required for a new account.")
                conn.execute(
                    "INSERT INTO users(username, role, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (username, role.value, hash_password(password), utc_now()),
                )
                return
            conn.execute("UPDATE users SET role = ? WHERE username = ?", (role.value, username))
            if password:
                conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(password), username))

    def verify_login(self, username: str, password: str) -> User | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            # Hash anyway so unknown usernames take as long as wrong passwords (no user probing).
            hash_password(password)
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return self._row_to_user(row)

    def delete_user(self, username: str) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.execute("DELETE FROM project_members WHERE username = ?", (username,))

    # -- projects ----------------------------------------------------------

    def list_projects(self) -> list[Project]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [self._row_to_project(row) for row in rows]

    def get_project(self, project_id: int) -> Project | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._row_to_project(row) if row else None

    def register_project(self, name: str, folder_name: str, created_by: str) -> Project:
        with self._session() as conn:
            existing = conn.execute("SELECT * FROM projects WHERE folder_name = ?", (folder_name,)).fetchone()
            if existing:
                return self._row_to_project(existing)
            cursor = conn.execute(
                "INSERT INTO projects(name, folder_name, created_by, created_at) VALUES (?, ?, ?, ?)",
                (name, folder_name, created_by, utc_now()),
            )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row_to_project(row)

    def unregister_project(self, project_id: int) -> None:
        """Remove a project from the registry. The folder on disk is left untouched."""
        with self._session() as conn:
            conn.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # -- memberships ---------------------------------------------------------

    def list_project_members(self, project_id: int) -> dict[str, Role]:
        """Username -> project role for one project."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT username, role FROM project_members WHERE project_id = ? ORDER BY username",
                (project_id,),
            ).fetchall()
        return {row["username"]: Role(row["role"]) for row in rows}

    def set_project_members(self, project_id: int, members: dict[str, Role]) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO project_members(project_id, username, role) VALUES (?, ?, ?)",
                [(project_id, username, role.value) for username, role in members.items()],
            )

    def set_project_member(self, project_id: int, username: str, role: Role) -> None:
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO project_members(project_id, username, role)
                VALUES (?, ?, ?)
                ON CONFLICT(project_id, username) DO UPDATE SET role = excluded.role
                """,
                (project_id, username, role.value),
            )

    def remove_project_member(self, project_id: int, username: str) -> None:
        with self._session() as conn:
            conn.execute(
                "DELETE FROM project_members WHERE project_id = ? AND username = ?",
                (project_id, username),
            )

    def role_in_project(self, username: str, project_id: int) -> Role | None:
        """Effective project role: Workspace Admins are Admin everywhere; otherwise the membership role."""
        if self.is_workspace_admin(username):
            return Role.ADMIN
        with self._session() as conn:
            row = conn.execute(
                "SELECT role FROM project_members WHERE project_id = ? AND username = ?",
                (project_id, username),
            ).fetchone()
        return Role(row["role"]) if row else None

    def projects_for_user(self, username: str) -> list[Project]:
        """Projects the user may open: all for Workspace Admins, memberships only for everyone else."""
        if self.is_workspace_admin(username):
            return self.list_projects()
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM projects p
                JOIN project_members m ON m.project_id = p.id
                WHERE m.username = ?
                ORDER BY p.name
                """,
                (username,),
            ).fetchall()
        return [self._row_to_project(row) for row in rows]

    def user_can_open(self, username: str, project_id: int) -> bool:
        return self.role_in_project(username, project_id) is not None

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            folder_name=row["folder_name"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )


class WorkspaceContext:
    def __init__(self, root_dir: Path, storage: WorkspaceStorage) -> None:
        self.root_dir = root_dir
        self.storage = storage

    def project_dir(self, project: Project) -> Path:
        return self.root_dir / project.folder_name

    def create_project(self, name: str, created_by: str) -> Project:
        name = name.strip()
        if not name:
            raise ValueError("Project name is required.")
        folder_name = sanitize_folder_name(name)
        if not folder_name:
            raise ValueError("Project name must contain at least one letter or digit.")
        project = self.storage.register_project(name, folder_name, created_by)
        (self.root_dir / folder_name).mkdir(parents=True, exist_ok=True)
        return project

    def discover_projects(self, created_by: str) -> list[Project]:
        """Register any subfolder that already contains a project database."""
        discovered: list[Project] = []
        known_folders = {project.folder_name for project in self.storage.list_projects()}
        for child in sorted(self.root_dir.iterdir()):
            if not child.is_dir() or child.name in known_folders:
                continue
            if (child / PROJECT_DB).exists():
                discovered.append(self.storage.register_project(child.name, child.name, created_by))
        return discovered


def sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", "_", cleaned)


def open_or_create_workspace(root_dir: Path) -> WorkspaceContext:
    """Open the shared root folder; a fresh workspace is seeded with the default admin account."""
    root_dir.mkdir(parents=True, exist_ok=True)
    storage = WorkspaceStorage(root_dir / WORKSPACE_DB)
    storage.initialize()
    storage.seed_default_admin()
    workspace = WorkspaceContext(root_dir=root_dir, storage=storage)
    workspace.discover_projects(created_by=DEFAULT_ADMIN_USERNAME)
    return workspace
