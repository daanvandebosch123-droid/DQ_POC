# DQTool architecture

This is the high-level architecture overview. For implementation procedures,
schema-change guidance and release checklists, see
[technical-documentation.md](technical-documentation.md).

## System shape

DQTool is a Python 3.11+ NiceGUI application that runs as one web-server
process. That process hosts both the browser UI and the in-process scheduler.

    Browser
       |
       v
    NiceGUI UI (web_app.py)
       |
       v
    Application services
       |------------------|
       v                  v
    Workspace SQLite   Per-project SQLite
       |                  |
       v                  v
    Users/projects     Connections, rules, groups,
                       schedules, runs and profiles
       |
       +--> Source systems: CSV/DuckDB, Oracle, ODBC databases

## Technology stack

| Technology | Responsibility |
| --- | --- |
| NiceGUI | Web UI, dialogs, tables and browser-session state |
| SQLite | Workspace and project metadata persistence |
| DuckDB | CSV previewing and CSV rule execution |
| oracledb | Oracle connectivity |
| pyodbc | SQL Server, DB2 and Sybase connectivity |
| zoneinfo / tzdata | Brussels-time scheduling |
| Ollama (optional) | Local profile/anomaly assistance |

## Application layers

| Layer | Main modules | Responsibilities |
| --- | --- | --- |
| Entry point | `app.py` | Starts the application |
| Presentation | `web_app.py` | NiceGUI pages, dialogs, table state, permission-aware actions, scheduler loop |
| Domain model | `models/entities.py` | Enums and dataclasses for all persisted/application objects |
| Workspace services | `services/workspace.py` | Users, password verification, workspaces, projects and memberships |
| Project services | `services/project.py`, `services/storage.py` | Project folders, SQLite schema, migrations and CRUD |
| Source services | `services/connectors.py` | Connection testing, previews, target discovery and source access |
| Execution services | `services/execution.py` | Rule SQL generation, evaluation, result evidence and run history |
| Scheduling services | `services/scheduling.py` | Cadence descriptions and next-run calculation |
| Analysis services | `services/profiling.py`, `services/ai.py` | Profiles, drift findings, rule ideas and optional local-AI summaries |

The UI calls services; services own persistence and source-access details. This
separation lets tests exercise rule, connection and schedule behaviour without
requiring a browser.

## Data boundaries

### Workspace data

The workspace SQLite database is shared across projects and contains:

- user accounts and hashed application passwords;
- workspaces and project registration;
- project membership and roles.

### Project data

Every project has a project directory and its own `dqtool_project.sqlite`.
The database contains:

- connections (without database passwords);
- rules, descriptions and JSON rule configuration;
- hierarchical rule groups;
- schedules;
- rule-run history, status, counts, runtime and scheduler attribution;
- source profiles.

The project folder contains related non-database files:

    <project>/
      dqtool_project.sqlite
      uploads/     CSV files copied into the project
      results/     Failed-row evidence files
      exports/     Generated exports

Connection passwords are stored locally for the Windows user outside project
SQLite. A project can therefore be shared without sharing a credential.

## Source and execution flow

    Rule / schedule / batch selection
                  |
                  v
    ExecutionService resolves connection and source configuration
                  |
        +---------+----------+
        |                    |
        v                    v
    CSV -> DuckDB       Database -> Oracle or pyodbc
        |                    |
        +---------+----------+
                  v
    Checked/failed counts and limited failed-row evidence
                  |
                  v
    RuleRun persisted in project SQLite
                  |
                  v
    Results and scheduler statistics displayed in UI

The execution result status is `passed`, `failed` or `error`. New runs
store `runtime_ms`. Scheduler-originated runs also store `schedule_id`,
which is how schedule history and pass-rate statistics are calculated.

The Dashboard reads the latest 100 runs for its current-state cards and
hotspots, and up to 1,000 runs for daily quality trends. It does not persist
aggregates: pass rate, run volume, failed-row volume and average runtime are
calculated from `RuleRun` history when the dashboard refreshes.

## Supported connections

| Source type | Access path |
| --- | --- |
| CSV | DuckDB, with files confined to the project uploads folder |
| Oracle | oracledb |
| SQL Server | pyodbc and a vendor ODBC driver |
| DB2 | pyodbc and IBM DB2 ODBC Driver |
| Sybase ASE | pyodbc |
| Sybase IQ / SQL Anywhere | pyodbc and SAP SQL Anywhere driver |

The external/native database driver is a deployment prerequisite. Installing
the Python package alone does not install the vendor's driver.

## Scheduling flow

Schedules use `Europe/Brussels` rather than UTC or the PC's local timezone
for their displayed daily/weekly time of day. Timestamps are persisted in UTC.

    Persisted schedule
          |
          v
    Background loop polls for due schedules
          |
          v
    Resolve rule or nested rule group
          |
          v
    Execute rules and save RuleRuns with schedule_id
          |
          v
    Calculate and save next Brussels-time occurrence

The scheduler operates only while the DQTool process is running. A production
deployment requiring guaranteed execution should run the application as a
service or use a durable external job runner.

## Profiling and suggested-rule flow

Profiling is advisory: it creates a snapshot and recommendations but never
changes rules on its own.

    Selected source
          |
          v
    ProfilingService calculates column statistics and content findings
          |
          +--> Save SourceProfile for drift comparison and trend charts
          |
          +--> Infer meaning (number, date/time, email, category)
          |
          v
    Build editable rule suggestions, grouped/filterable by field in the UI
          |
          v
    Open normal rule dialog with source and settings prefilled

Suggestions use observed completeness, cardinality, value ranges, lengths and
value shapes. Dutch identifier naming (for example `klantnummer`, `ordernummer`,
`nummer`, `nr`, `code`, and `sleutel`) is considered when identifying likely
key fields. The user reviews and saves every rule explicitly.

The same analysis layer produces separate GDPR review flags. They identify
potential personal data, Article 9 special-category signals, and Article 10
criminal-offence signals from names and high-confidence patterns. Flags contain
categories, reasons and counts only—never the matching raw values—and are
presented separately from anomaly findings because they require privacy review,
not a data-quality verdict.

## Security boundaries and operational assumptions

- Application passwords are salted PBKDF2-SHA256 hashes.
- Connection credentials are local-user secrets and are not stored in project
  metadata.
- Custom SQL rules are trusted input and should be restricted to authorised
  users.
- Source database accounts should normally be read-only.
- SQLite project databases, uploads and results must be backed up together.
- Browser-session storage, test runtime output and tool caches are local files
  excluded by `.gitignore`; they are not project data.

## Extensibility

New rules normally touch the domain model, rule dialog, execution service,
storage contract and tests. New connection types normally touch the connection
enum, dialog, `ConnectorService`, dependencies and connector tests. See the
[technical maintenance guide](technical-documentation.md#maintenance-guide) for
the exact change checklists.
