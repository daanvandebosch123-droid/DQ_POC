from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dqtool.models.entities import Role, WorkspaceRole
from dqtool.services.workspace import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    open_or_create_workspace,
    sanitize_folder_name,
)


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.workspace = open_or_create_workspace(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fresh_workspace_seeds_default_workspace_admin(self) -> None:
        user = self.workspace.storage.verify_login(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        self.assertIsNotNone(user)
        self.assertEqual(WorkspaceRole.WORKSPACE_ADMIN, user.role)

    def test_login_rejects_wrong_password(self) -> None:
        self.assertIsNone(self.workspace.storage.verify_login(DEFAULT_ADMIN_USERNAME, "wrong"))
        self.assertIsNone(self.workspace.storage.verify_login("ghost", "admin"))

    def test_new_user_requires_password(self) -> None:
        with self.assertRaises(ValueError):
            self.workspace.storage.upsert_user("alice", WorkspaceRole.MEMBER, password=None)

    def test_update_keeps_password_when_none_given(self) -> None:
        storage = self.workspace.storage
        storage.upsert_user("alice", WorkspaceRole.MEMBER, password="secret")
        storage.upsert_user("alice", WorkspaceRole.WORKSPACE_ADMIN, password=None)
        user = storage.verify_login("alice", "secret")
        self.assertIsNotNone(user)
        self.assertEqual(WorkspaceRole.WORKSPACE_ADMIN, user.role)

    def test_create_project_makes_folder(self) -> None:
        project = self.workspace.create_project("Sales Checks", created_by="admin")
        self.assertTrue((self.root / project.folder_name).is_dir())
        self.assertEqual("Sales_Checks", project.folder_name)

    def test_workspace_admin_sees_all_members_see_memberships_only(self) -> None:
        storage = self.workspace.storage
        storage.upsert_user("alice", WorkspaceRole.MEMBER, password="pw")
        project_a = self.workspace.create_project("A", created_by="admin")
        project_b = self.workspace.create_project("B", created_by="admin")
        storage.set_project_members(project_a.id, {"alice": Role.USER})

        admin_projects = {p.name for p in storage.projects_for_user(DEFAULT_ADMIN_USERNAME)}
        alice_projects = {p.name for p in storage.projects_for_user("alice")}

        self.assertEqual({"A", "B"}, admin_projects)
        self.assertEqual({"A"}, alice_projects)
        self.assertTrue(storage.user_can_open("alice", project_a.id))
        self.assertFalse(storage.user_can_open("alice", project_b.id))
        self.assertTrue(storage.user_can_open(DEFAULT_ADMIN_USERNAME, project_b.id))

    def test_role_in_project(self) -> None:
        storage = self.workspace.storage
        storage.upsert_user("alice", WorkspaceRole.MEMBER, password="pw")
        storage.upsert_user("bob", WorkspaceRole.MEMBER, password="pw")
        project = self.workspace.create_project("P", created_by="admin")
        storage.set_project_members(project.id, {"alice": Role.ADMIN, "bob": Role.USER})

        self.assertEqual(Role.ADMIN, storage.role_in_project("alice", project.id))
        self.assertEqual(Role.USER, storage.role_in_project("bob", project.id))
        self.assertIsNone(storage.role_in_project("ghost", project.id))
        # Workspace Admin is admin everywhere without an explicit membership.
        self.assertEqual(Role.ADMIN, storage.role_in_project(DEFAULT_ADMIN_USERNAME, project.id))

    def test_set_project_member_upserts_role(self) -> None:
        storage = self.workspace.storage
        storage.upsert_user("carol", WorkspaceRole.MEMBER, password="pw")
        project = self.workspace.create_project("Q", created_by="admin")
        storage.set_project_member(project.id, "carol", Role.USER)
        self.assertEqual(Role.USER, storage.role_in_project("carol", project.id))
        storage.set_project_member(project.id, "carol", Role.ADMIN)
        self.assertEqual(Role.ADMIN, storage.role_in_project("carol", project.id))
        storage.remove_project_member(project.id, "carol")
        self.assertIsNone(storage.role_in_project("carol", project.id))

    def test_discover_registers_existing_project_folders(self) -> None:
        legacy = self.root / "LegacyProject"
        legacy.mkdir()
        (legacy / "dqtool_project.sqlite").touch()
        discovered = self.workspace.discover_projects(created_by="admin")
        self.assertEqual(["LegacyProject"], [p.name for p in discovered])
        # Re-running does not duplicate.
        self.assertEqual([], self.workspace.discover_projects(created_by="admin"))

    def test_delete_user_removes_memberships(self) -> None:
        storage = self.workspace.storage
        storage.upsert_user("bob", WorkspaceRole.MEMBER, password="pw")
        project = self.workspace.create_project("P", created_by="admin")
        storage.set_project_members(project.id, {"bob": Role.USER})
        storage.delete_user("bob")
        self.assertIsNone(storage.get_user("bob"))
        self.assertEqual({}, storage.list_project_members(project.id))

    def test_legacy_roles_are_migrated(self) -> None:
        storage = self.workspace.storage
        with storage._session() as conn:  # noqa: SLF001 - deliberate low-level check
            conn.execute(
                "INSERT INTO users(username, role, password_hash, created_at) VALUES ('old_admin', 'Admin', 'x', 'now')"
            )
            conn.execute(
                "INSERT INTO users(username, role, password_hash, created_at) VALUES ('old_user', 'User', 'x', 'now')"
            )
        storage.initialize()
        self.assertEqual(WorkspaceRole.WORKSPACE_ADMIN, storage.get_user("old_admin").role)
        self.assertEqual(WorkspaceRole.MEMBER, storage.get_user("old_user").role)

    def test_sanitize_folder_name(self) -> None:
        self.assertEqual("My_Project-2", sanitize_folder_name("  My  Project-2! "))
        self.assertEqual("", sanitize_folder_name("!!!"))


if __name__ == "__main__":
    unittest.main()
