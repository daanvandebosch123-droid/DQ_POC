# DQTool enterprise architecture brief

This brief provides the technical information needed to produce DQTool context, container, deployment, data-flow, and security diagrams.

## Application summary

DQTool is a Windows-first Python web application for defining and executing data-quality checks against CSV files and enterprise databases. It supports team workspaces and projects, role-based access, source profiling, anomaly and drift detection, rule scheduling, result evidence, Excel/CSV exports, and optional AI assistance.

It is currently a single deployable process: the NiceGUI web UI and background scheduler run in the same Python process.

## Core container view

```text
Business users / data stewards / project admins
                |
                | HTTP (local by default; optional LAN access)
                v
+----------------------------------------------------------+
| DQTool application host (Windows)                        |
| Python 3.11+ / NiceGUI web server                        |
| Default: 127.0.0.1:8080                                  |
|                                                          |
| Web UI + authentication + role checks                    |
| Workspace/project services                               |
| Rule execution service                                   |
| Profiling / anomaly detection service                    |
| In-process scheduler (approximately every minute)        |
| Export service                                            |
| Optional Ollama AI client                                |
+---------------------+------------------+-----------------+
                      |                  |
          source reads|                  | local filesystem / SQLite
                      v                  v
        +--------------------------+  +-------------------------+
        | CSV / Oracle / SQL Server|  | Shared workspace folder |
        | DB2 / Sybase ASE / IQ    |  | Workspace + projects    |
        +--------------------------+  +-------------------------+
                      |
                      | optional AI HTTP API
                      v
              +----------------------+
              | Ollama model service |
              | local or remote      |
              +----------------------+
```

## Application components

| Component | Responsibility | Implementation |
| --- | --- | --- |
| NiceGUI web UI | Browser pages, user session, dashboards, preview, accounts, rules, results, schedules and profiling | `web_app.py` |
| Workspace service | Users, password verification, workspace/project registration, memberships and roles | `workspace.py` |
| Project/storage service | Project SQLite schema, migrations, and CRUD for rules, connections, groups, runs, schedules and profiles | `project.py`, `storage.py` |
| Connector service | Tests connections, discovers targets, previews data, executes source-specific queries | `connectors.py` |
| Execution service | Evaluates rules, persists metrics/status and writes bounded failed-row evidence | `execution.py` |
| Profiling service | Profiles columns, detects drift/content findings, identifies possible GDPR-sensitive data and derives editable rule ideas | `profiling.py` |
| Scheduling service | Calculates hourly/daily/weekly occurrences in Europe/Brussels time | `scheduling.py` |
| AI service | Calls Ollama for optional explanations and recommendations | `ai.py` |
| Export service | Produces failed-row CSV and anomaly/profile XLSX reports | `anomaly_export.py` |

## Integrations

| Integration | Protocol / driver | Purpose | Direction |
| --- | --- | --- | --- |
| Browser clients | HTTP; NiceGUI session/cookies | Access the application | Inbound |
| CSV files | Filesystem + DuckDB | Profiling, preview and rule evaluation | Read; browser uploads write files |
| Oracle | `oracledb` | Profiling, preview and rule execution | Read-only access recommended |
| SQL Server | ODBC via `pyodbc` | Profiling, preview and rule execution | Read-only access recommended |
| IBM DB2 | ODBC via `pyodbc` | Profiling, preview and rule execution | Read-only access recommended |
| Sybase ASE | ODBC via `pyodbc` | Profiling, preview and rule execution | Read-only access recommended |
| Sybase IQ / SQL Anywhere | ODBC via `pyodbc` | Profiling, preview and rule execution | Read-only access recommended |
| Ollama (optional) | HTTP API | AI explanations and recommendations | Outbound |
| Cloudflare Access (optional) | HTTP request headers and service token | Protects a remote Ollama endpoint | Outbound |

Native ODBC drivers must be installed on the DQTool host for SQL Server, DB2 and Sybase. Oracle uses the Python `oracledb` driver.

## Storage and data locations

```text
<workspace-root>/
  dqtool_workspace.sqlite
  <project-folder>/
    dqtool_project.sqlite
    uploads/
    results/
    exports/
```

| Location | Contents | Considerations |
| --- | --- | --- |
| `dqtool_workspace.sqlite` | Users, salted password hashes, projects, workspace/project memberships and roles | Back up with all project folders |
| `<project>/dqtool_project.sqlite` | Connection definitions without passwords, rules, groups, schedules, run history and profiles | Back up with project files |
| `<project>/uploads/` | CSV files uploaded through the browser; max 200 MB per file | May contain sensitive source data |
| `<project>/results/` | Failed-row evidence from rule runs | May contain sensitive source data |
| `<project>/exports/` | Generated CSV/XLSX reports | May contain sensitive source data |
| Windows Credential Manager | Per-user connection passwords and optional Cloudflare AI access tokens | Local to Windows host/user; not in SQLite |
| `%LOCALAPPDATA%\\DQTool\\settings.json` | Local application settings, including workspace selection, AI endpoint/model and session signing secret | Host-local configuration |
| NiceGUI session storage | Browser session state, including last-opened project | Not a system of record |

An important design constraint is that database passwords are stored per Windows user in Credential Manager. In a central-server deployment, the server process identity controls which local credential store is used.

## Data model

### Workspace level

- User: username, salted PBKDF2-SHA256 password hash and workspace-admin status.
- Project: display name and project-folder reference.
- Membership: user-to-project assignment and project role.

### Project level

- Connection: source type, host, port, service/database, driver, username, JSON configuration, owner and visibility.
- Rule: rule type, source reference, JSON configuration, owner and visibility.
- Rule group: hierarchical group with direct rule and child-group references.
- Schedule: target rule/group, cadence, enabled state, owner, last/next execution data.
- Rule run: status, timestamps, row counts, runtime, executor, optional schedule ID and failed-evidence path.
- Source profile: timestamped profile JSON keyed to source identity.

Rules and connection configuration are JSON. New keys must be optional when old projects are opened, and unknown keys should be preserved during edits.

## Operational flows

### Interactive rule execution

```text
User browser
  -> UI checks user/project access
  -> Rule + connection read from project SQLite
  -> Password read from Windows Credential Manager
  -> Connector reads target source
  -> Execution service evaluates rule
  -> Bounded failed-row evidence optionally written to results/
  -> RuleRun persisted to project SQLite
  -> UI displays status, counts, runtime and evidence
```

Supported rules include not-null, uniqueness/duplicates, data volume, value range, regex, length, allowed values, date validity, freshness, custom SQL, referential integrity and keyed comparison. Custom SQL is authorised-user functionality and should be governed accordingly.

### Profiling and anomaly detection

```text
User selects a source
  -> Profiling service reads source metadata/statistics
  -> Profile snapshot persisted in source_profiles
  -> Current snapshot compared with prior snapshot
  -> Drift, content and GDPR review findings generated
  -> User may export XLSX or review/edit suggested rules
```

Profiles include row count, data types, null/blank rates, distinct counts, min/max, averages, text lengths, frequencies, inferred meaning, and outlier/malformed-value signals. Rule ideas are advisory and never automatically saved.

### Scheduled execution

```text
In-process scheduler (approximately every minute)
  -> Enumerates projects
  -> Finds enabled due schedules
  -> Resolves rule/group
  -> Executes through Execution service
  -> Stores RuleRuns with schedule_id and executed_by=scheduler
  -> Calculates and stores next occurrence
```

Schedules run hourly, daily or weekly. Daily/weekly timing uses `Europe/Brussels`; timestamps are stored in UTC. Scheduling only occurs while the DQTool process is running.

### Optional AI flow

```text
Profile/anomaly metadata
  -> AI prompt builder
  -> Ollama HTTP endpoint
  -> Explanation, rule priorities and operational recommendations
  -> UI presentation only
```

The default AI configuration is Ollama model `qwen3:8b` at `http://localhost:11434`. The endpoint and model are configurable. The normal AI flows send profile statistics, drift findings, GDPR categories and deterministic rule ideas, not source rows, sampled values or finding messages. AI is optional and does not run, save or schedule rules. A separate opt-in path can send sample values and requires explicit data-governance review.

## Security architecture

- Local DQTool authentication; no built-in corporate SSO, LDAP, SAML or OIDC integration.
- Application passwords are salted PBKDF2-SHA256 hashes.
- Roles: Workspace Admin, Project Admin and Project User.
- Project objects support `private`, `shared` and specific-user sharing scopes.
- Database passwords and Cloudflare AI tokens are held in Windows Credential Manager, not SQLite or Git.
- Default bind address is `127.0.0.1`; LAN hosting requires explicit host configuration and a Windows Firewall rule.
- The application should not be publicly exposed over HTTP.
- Use read-only source database accounts wherever possible.
- Uploaded CSVs, failed-row evidence and exports can hold sensitive data; apply filesystem ACLs, retention, backup and secure-disposal controls.

## Deployment and configuration

### Current deployment model

- Windows host.
- Python 3.11+ and the DQTool dependencies, or a packaged equivalent.
- One application process hosting UI and scheduler.
- A shared workspace folder available to the application process identity.
- Network reachability to browser clients, source databases and optionally Ollama.
- Native ODBC drivers installed for relevant database types.

| Setting | Default / behaviour |
| --- | --- |
| `DQTOOL_HOST` | `127.0.0.1`; set `0.0.0.0` for LAN access |
| `DQTOOL_PORT` | `8080` unless overridden |
| Workspace folder | Selected in application setup and stored locally |
| AI endpoint | `http://localhost:11434` by default |
| AI model | `qwen3:8b` by default |
| Schedule timezone | `Europe/Brussels` |
| Persisted timestamps | UTC |

## Constraints and enterprise considerations

- UI, scheduling, profiling and execution share one process.
- There is no message broker, queue, worker pool, API gateway, load balancer or independent scheduler component.
- SQLite is appropriate for a smaller shared/team deployment but should be assessed for concurrent writes, network-share locking, backup consistency and high availability.
- Horizontal scaling is not currently suitable: local credential storage, in-process scheduling and SQLite state create node affinity and duplicate-execution risks.
- For unattended scheduling, run the application as a managed Windows service or equivalent always-on process.
- The current design does not include built-in disaster recovery, central monitoring/alerting or SIEM/audit integration.

## Diagrams to create

1. **System context diagram:** users, DQTool, source databases/files, Ollama, Windows Credential Manager and shared workspace storage.
2. **Container diagram:** single Python/NiceGUI process and its UI, workspace, connector, execution, profiling, scheduler, export and AI components.
3. **Deployment diagram:** Windows application host, workspace filesystem/SMB location, Credential Manager, ODBC drivers, local/remote Ollama, database network zones, browser clients and firewall boundaries.
4. **Data-flow diagram:** rule execution, profiling/anomaly processing, scheduled execution, CSV upload and AI metadata flow.
5. **Security/trust-boundary diagram:** browser/LAN, application host, workspace filesystem, credential store, source-data zone and optional AI endpoint; mark sensitive flows for credentials, CSV uploads, failed rows, exports and opt-in AI sample values.

## Decisions for enterprise architecture

- Confirm whether DQTool is a local tool, departmental central server or enterprise platform.
- Decide whether to replace local authentication with OIDC/SAML/Active Directory.
- Decide whether secrets should move from Credential Manager to an enterprise vault.
- Assess replacing SQLite with a managed shared database for concurrency, audit, backup and high availability.
- Decide whether a shared filesystem is appropriate for uploads/results/exports, or whether object/document storage is required.
- Decide whether the in-process scheduler must be replaced with a durable enterprise job runner.
- Confirm whether AI metadata may leave the application trust boundary and define the approved model endpoint.
- Define retention, encryption, malware scanning, DLP and access-control controls for uploaded files, evidence and exports.
- Define authorisation, database identities, timeouts, audit controls and allowed-query policy for custom SQL.
