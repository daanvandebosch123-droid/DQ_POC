# DQTool technical documentation

This guide explains the application's implementation for developers and operators.

## System architecture

DQTool is a Python 3.11+ NiceGUI web application for defining data-quality rules, executing them against files or databases, scheduling recurring checks, and reviewing the results.

| Layer | Location | Responsibility |
| --- | --- | --- |
| User interface | `src/dqtool/web_app.py` | NiceGUI pages, dialogs, tables, access checks and scheduler startup |
| Services | `src/dqtool/services/` | Connections, execution, persistence, scheduling, profiling, workspace/project management |
| Domain model | `src/dqtool/models/entities.py` | Objects such as connections, rules, groups, schedules and rule runs |

Start the server with:

    python -m dqtool.app

The user interface and background scheduler are hosted by the same Python process.

## Repository layout

    src/dqtool/
      app.py                 Application entry point
      web_app.py             Main NiceGUI interface and scheduler loop
      models/entities.py     Domain dataclasses and enums
      services/
        connectors.py        CSV, Oracle and ODBC connectivity
        execution.py         Rule evaluation and result creation
        storage.py           SQLite persistence and schema migrations
        scheduling.py        Brussels-time schedule calculation
        workspace.py         Users, workspaces and membership
        project.py           Project metadata and directories
        profiling.py         Source profiling and anomaly support
        ollama.py            Optional local Ollama integration
    tests/                   Automated tests
    docs/                    Documentation

## Dependencies and setup

Dependencies are declared in `pyproject.toml`.

| Package | Purpose |
| --- | --- |
| NiceGUI | Browser UI and web server |
| DuckDB | Query engine for CSV sources |
| oracledb | Oracle connectivity |
| pyodbc | SQL Server, DB2 and Sybase connectivity |
| tzdata | Timezone support, including Windows |
| pytest / ruff | Tests and static checking |

Typical local setup:

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install .
    pip install -e ".[dev]"
    python -m dqtool.app

`pyodbc` is the Python bridge only. Every ODBC database also needs its vendor's native ODBC driver installed and registered on the Windows machine running DQTool.

## Storage and project structure

DQTool separates central workspace data from project data.

### Workspace database

The workspace service owns a central SQLite database containing:

- application users and password hashes;
- workspaces and projects;
- workspace/project memberships and roles.

The initial bootstrap account is `admin/admin` on a fresh installation. Change this before allowing other users access.

### Project database

Every project has its own SQLite database, normally `dqtool_project.sqlite`, containing:

- connections;
- rules and rule-group membership;
- schedules;
- execution history (`rule_runs`);
- source profiles.

The project directory also contains:

    <project>/
      dqtool_project.sqlite
      uploads/     Uploaded CSV files
      results/     Failed-row result files
      exports/     Generated exports

CSV uploads are copied into the project's `uploads` directory. The picker is restricted to that directory, which keeps file connections portable inside the project. The UI shows only the selected filename, while the stored configuration retains the server-side path.

### Schema migrations

`StorageService` creates missing tables and applies additive migrations when a project opens. Existing migrations add rule descriptions, nested group membership, `schedule_id` and `runtime_ms` to rule-run history. Back up project databases before manual modification.

## Core model

| Object | Purpose |
| --- | --- |
| Connection | A reusable CSV or database source definition |
| Rule | A quality check with type-specific JSON configuration and optional description |
| RuleGroup | A named container for rules and nested groups |
| RuleRun | One rule execution, including status, timestamps, counts and runtime |
| Schedule | A recurring execution instruction |
| SourceProfile | Persisted metadata/statistics for a source |

Rules and groups have owner and visibility fields. UI actions are filtered using the current user's workspace/project role and each object's visibility.

## Security and credentials

Application-user passwords are salted PBKDF2-SHA256 hashes stored in the workspace database.

Connection passwords are kept separately in a local JSON secrets store under the current Windows user's local application-data directory. The key includes the application username and connection name, allowing a project to share a connection definition without saving its password in project SQLite.

This is appropriate for a local/internal proof of concept. A production deployment should use a credential manager or managed secret vault, encrypt secrets at rest, and use database accounts with minimum required permissions.

## Connection subsystem

`ConnectorService` tests connections, lists available targets, samples data, and executes source-specific queries.

| Connection type | Implementation |
| --- | --- |
| CSV | DuckDB reads a file in project uploads |
| Oracle | `oracledb` |
| SQL Server | `pyodbc` and an installed SQL Server ODBC driver |
| DB2 | `pyodbc` and IBM DB2 ODBC Driver |
| Sybase ASE | `pyodbc` and Adaptive Server Enterprise settings |
| Sybase IQ / SQL Anywhere | `pyodbc` and the SAP SQL Anywhere driver |

The **Test connection** action uses the values currently entered in the dialog and does not require a save. For an existing connection, a newly typed password overrides the stored local password; otherwise the stored password is used.

### Sybase IQ / SQL Anywhere

The connection form distinguishes ASE from IQ / SQL Anywhere. IQ uses:

- the installed ODBC driver name (commonly `Sybase IQ`);
- host and port;
- **IQ Server Name**, passed to SQL Anywhere as `ENG`;
- optional database name, passed as `DBN`;
- username and password.

For SQL Anywhere, server name and database name are separate. “Database server not found” generally means host, port or server name is wrong. “Specified database not found” normally means the optional database name is wrong; leaving it empty is useful if the external ODBC tool connects without a database name.

## Rule execution

`ExecutionService` evaluates rules and persists a `RuleRun` for every result.

Execution flow:

    User, schedule, or batch action
              |
              v
    Resolve connection, source and local credential
              |
              v
    Generate source-specific SQL / DuckDB query
              |
              v
    Count checked and failed rows; write limited row evidence if needed
              |
              v
    Measure elapsed time with time.perf_counter()
              |
              v
    Store RuleRun: passed, failed, or error

Type-specific rule settings are stored as JSON in the project database. The execution service handles completeness/not-null, uniqueness, referential-integrity, custom SQL and other UI-supported rule variants.

Each newly created run stores `runtime_ms`, which is displayed on the Results tab. Older runs can have no runtime because that field was added after initial releases. Failed-row files, where produced, are saved in the project's `results` directory.

The Rules tab supports multi-selection. **Run selected** is intentionally visible only once one or more rules are selected. A rule can be moved between hierarchical groups without changing its own definition.

## Results

Execution history resides in `rule_runs` in the project database. The Results page reads that data and shows rule, status, start time, checked rows, failed rows and runtime.

Selecting a rule group resolves all rules in that group and its nested groups, then combines their executions in the “Executions of the selected rule” section. This makes a group an operational view across related checks.

## Scheduler

Schedules define a cadence plus a selected rule set or rule group. `services/scheduling.py` uses `ZoneInfo("Europe/Brussels")` explicitly, so daily and weekly schedules follow Brussels time rather than UTC or the PC's configured timezone. Daylight-saving changes are handled by the timezone database.

    Saved schedule
          |
          v
    web_app.py background loop polls roughly every minute
          |
          v
    Due schedule resolves its rules/group
          |
          v
    ExecutionService creates RuleRun records tagged with schedule_id
          |
          v
    Next Brussels-time occurrence is calculated and saved

The scheduler works only while the DQTool server process is running. A stopped process misses scheduled executions. For dependable production scheduling, run DQTool as a Windows service or use a durable external job runner.

The Schedules page builds execution statistics from `RuleRun` records associated with each `schedule_id`, including historical executions, pass rate, counts and status. Runs created before schedule attribution was introduced cannot be retroactively linked automatically.

## Profiling and AI support

`ProfilingService` collects source metadata and statistics, persists them as `SourceProfile` records, and supports drift/anomaly comparisons.

The optional Ollama service uses a locally running Ollama endpoint (normally `http://localhost:11434`) and the configured model. It sends profile/anomaly context rather than full source datasets. Rule execution does not depend on Ollama.

## UI and state

`web_app.py` contains the NiceGUI pages, dialogs, selection state and access checks. UI code should call services rather than directly edit SQLite tables.

Notable UI design choices:

- connection forms can test unsaved values;
- CSV file selection is searchable and confined to the uploads directory;
- normal tables hide internal IDs; IDs are available where editing requires them;
- groups use expandable tree rows;
- rule descriptions use the standard form input styling.

NiceGUI state is browser-session based. Rule execution is currently in the application process, meaning large source queries may affect UI responsiveness. A larger deployment should move execution to workers and let the UI poll for progress.

## Testing and quality checks

Run automated tests:

    .\.venv\Scripts\python.exe -m pytest -q

Run static analysis:

    .\.venv\Scripts\python.exe -m ruff check src tests

Tests use temporary project/workspace data and mocks for external database drivers. Real database integration tests should use non-production credentials and sources.

## Operations and extension

Operational recommendations:

- use read-only database accounts for quality checks;
- back up the project SQLite database, uploads and results together;
- keep ODBC driver versions consistent across host machines;
- treat custom SQL rules as trusted administrator input;
- keep the server running for schedules;
- move local secrets and in-process scheduling to managed infrastructure for production.

To add a rule type, define its model/configuration, add dialog fields, implement evaluation in `ExecutionService`, and add pass/fail/error tests. To add a connection type, extend the enum/dialog and `ConnectorService`, add connection-string tests, and document its native-driver prerequisite.

## Maintenance guide

This section is the working guide for maintainers making code changes. Make a small
change in the correct layer, test that layer, and then run the wider checks before
merging or releasing.

### Change-routing map

Use this table to find the usual starting point for a change. Most user-facing
features need changes in more than one row.

| Requested change | Primary code | Usually also change |
| --- | --- | --- |
| New/changed screen, button, dialog, table or selection behaviour | `web_app.py` | Relevant service tests |
| New field on a rule, run, group, connection or schedule | `models/entities.py` | `storage.py`, UI dialog/table, migration tests |
| New rule logic or altered pass/fail outcome | `services/execution.py` | Rule dialog, entities, execution tests |
| New database technology or connection option | `services/connectors.py` | Entities, connection dialog, pyproject, connector tests |
| Saved data/schema change | `services/storage.py` | Entity mapping, backwards-compatibility tests |
| New cadence or time calculation | `services/scheduling.py` | Schedule UI and scheduling tests |
| Login, users, workspace/project access | `services/workspace.py` | UI permission checks and workspace tests |
| New profile, anomaly or AI summary | `services/profiling.py` / `ollama.py` | UI presentation and profile tests |

### Non-negotiable change rules

1. Keep persistence, execution and connection logic in services. Do not put SQL,
   direct SQLite writes, or secret handling in `web_app.py`.
2. Keep browser-facing UI code in `web_app.py`. It should assemble inputs,
   validate user-facing requirements, call a service and refresh state.
3. Add or update an entity before adding a new persisted field. This makes the
   storage contract explicit.
4. Any project-database schema change must work against both a new project and
   an existing project database.
5. Preserve existing values on edit. An empty password in an edit dialog means
   “keep the locally stored password”, not “erase it”.
6. Never add database credentials, real source extracts, user secrets, or
   generated result files to Git.
7. Keep all schedule calculations in Brussels time unless the product
   requirement explicitly changes. Do not use the operating system's local
   timezone as a substitute.

### Standard implementation workflow

For a normal feature or bug fix:

1. Read the relevant entity, service and current tests before editing.
2. Identify the persisted contract: model field, JSON configuration key, or
   SQLite column.
3. Implement the service-level behaviour first.
4. Add/adjust the UI only after the service contract is clear.
5. Add focused tests that reproduce the reported behaviour and protect the new
   behaviour.
6. Run Ruff and the focused test file. Run the full suite before handoff.
7. Review `git diff` and `git status`; stage only source, tests and
   documentation that belong to the change.

Useful commands on Windows:

    .\.venv\Scripts\python.exe -m pytest tests\test_name.py -q
    .\.venv\Scripts\python.exe -m pytest -q
    .\.venv\Scripts\python.exe -m ruff check src tests
    git diff --check
    git status --short

## Safe data-model changes

### Adding a field to an existing entity

For example, adding a new property to `Rule`:

1. Add the dataclass field in `models/entities.py`. Use a safe default if
   older callers can construct the object.
2. In `Storage.initialize()`, add an idempotent migration that detects
   whether the SQLite column exists before issuing `ALTER TABLE`.
3. Update the insert/update statement in `save_rule()`.
4. Update `_row_to_rule()` so both newly written and migrated rows map back
   to the entity correctly.
5. Add the field to export/import metadata when it is part of the project
   contract.
6. Add it to the create/edit form and to the appropriate read-only view.
7. Test a fresh database and a database representing the previous schema.

Do not change an existing column's meaning in place. Prefer a new column plus a
migration path. SQLite is used locally, so schema migrations run when the
project database is opened; a broken migration can prevent users from opening a
project.

### JSON configuration fields

Rule settings and connection-specific settings are stored in JSON. When adding a
JSON key:

- use a clear, stable snake_case name;
- treat it as optional when reading older data;
- validate it before it is used to build SQL or a connection string;
- provide a default in the UI;
- preserve unrelated keys when an existing object is edited.

Avoid silently changing the meaning of a JSON key. If a breaking change is
necessary, migrate the old configuration during load or save and test both
versions.

### Deleting or renaming fields

This is higher risk than adding a field. First search the repository for the
field name, including tests and user-facing labels. Keep a compatibility read
path for old project databases where practical. Do not remove a database column
until an explicit migration and data-retention decision have been made.

## Adding or changing a rule type

Rule types cross the UI, model, execution and results layers. A reliable
implementation sequence is:

1. Add the enum value in `RuleType`.
2. Define the required `config_json` keys and their defaults.
3. Add conditional fields and validation to the rule create/edit dialog.
4. Update `ExecutionService._build_rule_sql()` for SQL-capable checks, or add
   a dedicated execution path if the rule cannot be represented as one query.
5. Make sure `_status_for_counts()` receives the expected checked and failed
   counts and threshold configuration.
6. Ensure failed-row collection remains bounded; do not load an unbounded
   failing dataset into memory.
7. Include a clear description in `_summary()` or the result metadata if a
   new outcome needs explanation.
8. Test CSV/DuckDB behaviour and every supported database dialect that needs
   different syntax.

The execution service has two main paths:

- CSV and local relations use DuckDB.
- Database sources use native SQL through `ConnectorService`.

Referential-integrity checks have separate logic because they may compare two
sources and may need batched key scanning. Do not force a new cross-source check
through the simple one-query rule path without considering scale and memory use.

### Rule execution contract

Every execution must produce a meaningful `RuleRun`, including when an
exception occurs. Maintain these fields:

| Field | Maintenance expectation |
| --- | --- |
| `status` | Use the existing `passed`, `failed`, or `error` meanings consistently |
| `checked_rows` | Number of rows evaluated, when available |
| `failed_rows` | Number of rows that violated the rule, when available |
| `started_at` / `finished_at` | UTC ISO timestamps |
| `runtime_ms` | Measured elapsed execution time for new runs |
| `schedule_id` | Set for scheduler-originated runs |
| result file path | Only when failed-row evidence is written |

Do not report a failed execution as passed merely because the query returned no
rows after an error. Errors must remain visible to operators in the Results and
Schedules pages.

## Adding or changing a connection type

Connection implementations live in `ConnectorService`. A new type needs more
than a connection string: it must support testing, target discovery, previewing
and rule execution where applicable.

Implementation checklist:

1. Add the type to `ConnectionType`.
2. Add the type-specific fields and help text to the connection dialog.
3. Add defaults and validation in the dialog save path.
4. Extend `database_dialect()`, `connect_database()` and any source
   discovery branches in `ConnectorService`.
5. Add the native Python dependency to `pyproject.toml` if needed. Document
   any external ODBC/native client installation separately.
6. Ensure `test_connection()` returns a concise, actionable message and
   always closes opened connections/cursors.
7. Add unit tests that assert the generated connection string and error
   handling without requiring a real database.
8. Test preview, column listing and a simple rule against a safe test source.

### ODBC maintenance

`ODBC_SETTINGS` in `connectors.py` centralizes driver defaults, ports and
connection-string patterns for SQL Server, DB2 and Sybase. Keep vendor-specific
syntax there rather than scattering it through UI code.

When changing ODBC connection strings:

- never log the password;
- preserve braces around driver names;
- ensure host/port delimiters are correct for the driver;
- test an entered password override and a stored-password path;
- retain the original exception context where possible, because vendor error
  messages are usually the most useful diagnosis.

For Sybase IQ / SQL Anywhere, `ENG` is the server/engine name and `DBN` is
the optional database name. Changing that mapping breaks installations that can
connect through the SAP ODBC tool.

## Changing groups, selections and tables

Rule groups are nested. Their storage contract uses direct rule IDs plus child
group IDs. The UI resolves nested membership to display a group as one logical
set.

When changing group behaviour:

1. Update group traversal/resolution logic before changing display code.
2. Guard against cycles in new traversal code; a corrupt or manually edited
   project database must not cause unbounded recursion.
3. Keep move operations transactional: add to the target and remove from
   source groups as one storage operation.
4. Test direct rules, nested rules, empty groups and a group with multiple
   subgroups.
5. Verify that selection in Results produces the combined history of all
   resolved rules.

Tables use internal identifiers for state and actions even when the ID column is
hidden. Do not infer identity from a displayed name: names can change and may
be deduplicated by storage.

## Scheduler maintenance

All persisted run timestamps are UTC ISO values. The schedule calculator
interprets cadence times in `Europe/Brussels`, then the application stores the
result in UTC. UI formatting converts schedule times back to Brussels time.

When changing scheduling:

1. Change `compute_next_run()` and its tests first.
2. Test hourly, daily and weekly cadences.
3. Test a transition near Central European summer-time changes.
4. Confirm a due schedule executes once and receives a newly calculated next
   run, rather than repeatedly running in the polling loop.
5. Preserve `schedule_id` on every scheduler-created `RuleRun`; the
   scheduler statistics depend on it.

The polling interval is approximately one minute, so a scheduled execution can
start up to roughly one polling interval after its configured minute. This is
expected behaviour, not a timezone defect.

## UI maintenance practices

`web_app.py` is intentionally the composition layer, but it is large. Keep
new code maintainable by following these conventions:

- Extract reusable formatting, selection and dialog helpers instead of copying
  callbacks.
- Refresh only the affected view after save/delete/run when possible.
- Use UI notifications for expected validation or connection errors; preserve
  the technical error details for operators.
- Keep a form field's label, helper text and validation aligned with its
  service-level contract.
- Use the existing table styling and controls. A raw HTML input or differently
  styled text area should be introduced only when there is a concrete UX need.
- Confirm that buttons requiring a selection are hidden or disabled consistently
  with the page's existing convention.
- Check both the create and edit versions of a dialog. It is common to fix only
  one path accidentally.

Manual smoke test after a UI change:

1. Open the affected tab with a normal project user and with an administrator.
2. Create a new object, edit it, cancel an edit, and delete it if that action
   is available.
3. Reload the browser page and verify the persisted result.
4. Check a narrow browser width and a normal desktop width.
5. Confirm an error message is understandable and does not expose secrets.

## Troubleshooting playbook

| Symptom | First checks | Likely area |
| --- | --- | --- |
| Connection test fails | Driver installed, exact host/port, driver name, username, password, vendor error text | `connectors.py`, external ODBC/client setup |
| Sybase says server not found | IQ server name, host, port, TCP/IP accessibility | Sybase IQ `ENG` settings |
| Sybase says database not found | Database field/DBN; test it empty if external tool did not need it | Sybase IQ `DBN` settings |
| Rule is erroring | Connection test, source columns, generated rule configuration, results error text | `execution.py`, `connectors.py` |
| Rule gives unexpected pass/fail | Checked/failed counts, thresholds, SQL dialect and sample rows | `_build_rule_sql()`, `_status_for_counts()` |
| Scheduled run did not occur | Server was running, next-run value, Brussels time, schedule enabled and due | `scheduling.py`, scheduler loop |
| Schedule has no statistics | Confirm runs have matching `schedule_id`; older runs are not attributed | scheduler execution path |
| CSV cannot be found | Verify it exists under project `uploads` and connection config references it | CSV picker and `csv_connection_file()` |
| Changes disappeared after restart | Inspect project selected, project SQLite path, save action and browser notification | storage/UI integration |

For a reported execution problem, preserve the rule configuration, connection
type, timestamp and full non-secret error message before making changes. Those
four pieces normally identify whether the failure is in source access, query
generation, scheduling or UI state.

## Release checklist

Before committing:

- [ ] Run Ruff on `src` and `tests`.
- [ ] Run the focused tests, then the full test suite.
- [ ] Run `git diff --check`.
- [ ] Review the diff for accidental credentials, local file paths and generated
      outputs.
- [ ] Verify all model, storage, service and UI layers were updated for any new
      persisted field.
- [ ] Add release notes or update this document when the operational behaviour
      changes.

Before deployment:

- [ ] Back up workspace and affected project databases plus uploads/results.
- [ ] Confirm required Python and native ODBC drivers are installed.
- [ ] Confirm the production account has the intended database permissions.
- [ ] Test a connection, one manual rule run and one scheduled rule using a
      non-destructive source.
- [ ] Confirm the DQTool process will remain running if schedules are required.

### Git hygiene

Generated test fixtures and runtime results can appear as modified or deleted
files after tests. Do not include them in a feature commit unless the test
fixture itself was deliberately changed. Likewise, do not commit NiceGUI local
storage files, local SQLite databases, uploads, exports, result CSVs, virtual
environments, or secret stores.
