from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from dqtool.services import project


class ConnectionSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.legacy_secrets: dict[str, object] = {}
        self.saved_legacy_payloads: list[dict[str, object]] = []
        self.paths = patch.object(project, "SECRETS_PATH", Path.cwd() / "connection-secrets-test-missing.json")
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.keyring = patch.multiple(
            project.keyring,
            get_password=self._get_password,
            set_password=self._set_password,
            delete_password=self._delete_password,
        )
        self.keyring.start()
        self.addCleanup(self.keyring.stop)
        self.secrets = patch.multiple(
            project,
            load_secrets=lambda: self.legacy_secrets,
            save_secrets=lambda payload: self.saved_legacy_payloads.append(payload.copy()),
        )
        self.secrets.start()
        self.addCleanup(self.secrets.stop)

    def _get_password(self, service_name: str, username: str) -> str | None:
        return self.passwords.get((service_name, username))

    def _set_password(self, service_name: str, username: str, password: str) -> None:
        self.passwords[(service_name, username)] = password

    def _delete_password(self, service_name: str, username: str) -> None:
        self.passwords.pop((service_name, username), None)

    def test_save_and_get_use_os_credential_store(self) -> None:
        project.save_connection_secret("Warehouse", "alice", "password")

        self.assertEqual("password", project.get_connection_secret("Warehouse", "alice"))
        self.assertEqual({}, self.legacy_secrets)

    def test_get_migrates_legacy_json_secret_and_removes_it(self) -> None:
        self.legacy_secrets = {"oracle_passwords": {"alice:Warehouse": "old-password"}}

        self.assertEqual("old-password", project.get_connection_secret("Warehouse", "alice"))
        self.assertEqual("old-password", self.passwords[(project.KEYRING_SERVICE_NAME, "alice:Warehouse")])
        self.assertEqual({}, self.legacy_secrets)

    def test_migration_preserves_other_legacy_entries(self) -> None:
        self.legacy_secrets = {"oracle_passwords": {"alice:Warehouse": "old-password", "bob:Warehouse": "other"}}

        project.get_connection_secret("Warehouse", "alice")

        self.assertEqual({"oracle_passwords": {"bob:Warehouse": "other"}}, self.legacy_secrets)
        self.assertEqual(self.legacy_secrets, self.saved_legacy_payloads[-1])

    def test_delete_removes_os_credential(self) -> None:
        project.save_connection_secret("Warehouse", "alice", "password")
        project.delete_connection_secret("Warehouse", "alice")

        self.assertIsNone(project.get_connection_secret("Warehouse", "alice"))

    def test_ollama_access_credentials_use_os_credential_store(self) -> None:
        project.save_ollama_access_credentials("client-id", "client-secret")

        self.assertEqual(("client-id", "client-secret"), project.get_ollama_access_credentials())

    def test_ollama_access_credentials_missing_returns_none(self) -> None:
        self.assertIsNone(project.get_ollama_access_credentials())

    def test_ollama_access_credentials_partial_returns_none(self) -> None:
        project.keyring.set_password(project.KEYRING_SERVICE_NAME, "ollama_cf_access_client_id", "client-id")

        self.assertIsNone(project.get_ollama_access_credentials())

    def test_ollama_access_credentials_delete_removes_both(self) -> None:
        project.save_ollama_access_credentials("client-id", "client-secret")
        project.delete_ollama_access_credentials()

        self.assertIsNone(project.get_ollama_access_credentials())
