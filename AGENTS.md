# DQTool contributor guidance

## Purpose

DQTool is a Python/NiceGUI data-quality application. Keep changes small,
service-oriented and safe for existing SQLite project databases.

## Local commands

- Start the application: `./.venv/Scripts/python.exe -m dqtool.app`
- Run all tests: `./.venv/Scripts/python.exe -m pytest -q`
- Run a focused test: `./.venv/Scripts/python.exe -m pytest tests/test_name.py -q`
- Run linting: `./.venv/Scripts/python.exe -m ruff check src tests`
- Check whitespace: `git diff --check`

## Code ownership

- `src/dqtool/web_app.py`: NiceGUI pages, dialogs, tables, browser state and
  background scheduler startup. UI code calls services; it does not directly
  write SQLite data.
- `src/dqtool/models/entities.py`: domain dataclasses and enums.
- `src/dqtool/services/storage.py`: project SQLite schema, migrations and
  CRUD operations.
- `src/dqtool/services/workspace.py`: users, projects and memberships.
- `src/dqtool/services/connectors.py`: CSV, Oracle and ODBC connection,
  preview and target-discovery logic.
- `src/dqtool/services/execution.py`: rule evaluation and `RuleRun`
  persistence.
- `src/dqtool/services/scheduling.py`: cadence and next-run calculations.

## Data and migration rules

- Add a model field before persisting it.
- Every project-database schema change must be additive and safe for existing
  projects. Update the entity mapper, save query and migration together.
- Rules and connection configuration are JSON. Treat new JSON keys as optional
  when reading older projects and preserve unknown keys on edit.
- Timestamps are stored in UTC. Schedule times use `Europe/Brussels`; do not
  substitute the PC timezone.
- Scheduler-originated runs must retain `schedule_id`; new execution runs
  should retain `runtime_ms`.

## Security and source access

- Do not put database passwords in project SQLite, source code, tests or Git.
- Never log credentials or full connection strings containing a password.
- Keep custom SQL restricted to authorised users and prefer read-only source
  database accounts.
- Do not expose the application publicly over HTTP.

## UI conventions

- Use existing NiceGUI controls and styling; avoid introducing inconsistent raw
  HTML controls.
- Check both create and edit flows after changing a dialog.
- Keep actions that require a selection hidden or disabled until selection
  exists, following the page's existing behaviour.
- Normal tables hide internal IDs; use IDs only where editing or diagnostics
  require them.

## Verification and Git hygiene

- Add or update focused tests for behaviour changes.
- Run Ruff, relevant tests and `git diff --check` before handoff.
- Review `git status` before staging.
- Do not commit `.venv`, local SQLite databases, secrets, NiceGUI storage,
  uploads, exports, result CSVs or generated runtime test artifacts.
- Preserve unrelated user changes in a dirty working tree.

For detailed architecture and maintenance procedures, read:

- `docs/architecture.md`
- `docs/technical-documentation.md`
