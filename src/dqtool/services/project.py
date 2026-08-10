from __future__ import annotations

import json
import os
import secrets as secrets_module
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import PasswordDeleteError

from dqtool.services.storage import Storage

APP_HOME = Path.home() / "AppData" / "Local" / "DQTool"
SETTINGS_PATH = APP_HOME / "settings.json"
SECRETS_PATH = APP_HOME / "secrets.json"
PROJECT_DB = "dqtool_project.sqlite"
KEYRING_SERVICE_NAME = "DQTool"


class ProjectContext:
    def __init__(self, project_dir: Path, storage: Storage, read_only: bool = False) -> None:
        self.project_dir = project_dir
        self.storage = storage
        self.read_only = read_only

    @property
    def results_dir(self) -> Path:
        return self.project_dir / "results"

    @property
    def exports_dir(self) -> Path:
        return self.project_dir / "exports"

    @property
    def uploads_dir(self) -> Path:
        return self.project_dir / "uploads"


def ensure_app_home() -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict[str, Any]:
    ensure_app_home()
    if not SETTINGS_PATH.exists():
        return {}
    # Windows PowerShell can save JSON with a UTF-8 BOM; accept both UTF-8 forms.
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))


def save_settings(payload: dict[str, Any]) -> None:
    ensure_app_home()
    SETTINGS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def open_or_create_project(project_dir: Path) -> ProjectContext:
    """Open a project folder. Users and permissions live at the workspace level."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "results").mkdir(exist_ok=True)
    (project_dir / "exports").mkdir(exist_ok=True)
    (project_dir / "uploads").mkdir(exist_ok=True)
    storage = Storage(project_dir / PROJECT_DB)
    storage.initialize()
    return ProjectContext(project_dir=project_dir, storage=storage)


def get_or_create_storage_secret() -> str:
    """Random per-installation secret used to sign browser session cookies."""
    settings = load_settings()
    secret = settings.get("storage_secret")
    if not secret:
        secret = secrets_module.token_hex(32)
        settings["storage_secret"] = secret
        save_settings(settings)
    return secret


def current_username() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def load_secrets() -> dict[str, Any]:
    ensure_app_home()
    if not SECRETS_PATH.exists():
        return {}
    return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))


def save_secrets(payload: dict[str, Any]) -> None:
    ensure_app_home()
    SECRETS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_connection_secret(connection_name: str, username: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE_NAME, _connection_secret_key(connection_name, username), password)


def get_connection_secret(connection_name: str, username: str) -> str | None:
    secret_key = _connection_secret_key(connection_name, username)
    password = keyring.get_password(KEYRING_SERVICE_NAME, secret_key)
    if password is not None:
        return password

    # Migrate passwords written by pre-keyring versions only after the new
    # Windows Credential Manager entry has been saved successfully.
    secrets = load_secrets()
    legacy_password = secrets.get("oracle_passwords", {}).get(secret_key)
    if legacy_password is None:
        return None
    keyring.set_password(KEYRING_SERVICE_NAME, secret_key, legacy_password)
    _remove_legacy_connection_secret(secrets, secret_key)
    return legacy_password


def delete_connection_secret(connection_name: str, username: str) -> None:
    """Delete a password from the local OS credential store if it exists."""
    try:
        keyring.delete_password(KEYRING_SERVICE_NAME, _connection_secret_key(connection_name, username))
    except PasswordDeleteError:
        pass


def _connection_secret_key(connection_name: str, username: str) -> str:
    return f"{username}:{connection_name}"


def _remove_legacy_connection_secret(secrets: dict[str, Any], secret_key: str) -> None:
    passwords = secrets.get("oracle_passwords")
    if not isinstance(passwords, dict):
        return
    passwords.pop(secret_key, None)
    if not passwords:
        secrets.pop("oracle_passwords", None)
    if secrets:
        save_secrets(secrets)
    elif SECRETS_PATH.exists():
        SECRETS_PATH.unlink()
