# DQTool

DQTool is a Windows-first Python application for defining and running data quality checks against Oracle, SQL Server, DB2, Sybase (SAP ASE), and CSV datasets inside a shared team project. Besides pass/fail rules, it also profiles sources over time to flag drift (row count jumps, null spikes, vanished columns, shifted averages), optionally explained in plain language by a locally running AI model.

## Feature overview

- NiceGUI web UI: dashboard, connections, rules, schedules, results, anomaly detection, accounts, and source preview
- Shared project metadata stored in a SQLite file inside a shared workspace folder
- `Workspace Admin` / `Project Admin` / `Project User` roles, with per-item visibility and ownership
- Connectors for Oracle, SQL Server, DB2, Sybase (SAP ASE), and CSV
- CSV files can be uploaded straight from the browser (not just browsed from the server's disk)
- Built-in rules plus custom SQL rules, including SQL that runs against a whole connection
- Rule groups: nest rules and subgroups into a tree, and run an entire group in one click
- Checkboxes in the Rules tab to run an arbitrary hand-picked set of rules in a single batch, independent of groups
- Schedules: run a rule or rule group automatically on an hourly/daily/weekly cadence while the app is running
- Anomaly detection: snapshot a source's profile over time and flag drift, with an optional local-AI explanation
- Failed-row previews in the app and export to CSV
- JSON import/export for rules, datasets, and connections
- Central-server mode so a team can share one running instance over the network

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

CSV, Oracle, SQL Server, DB2, and Sybase support install with the standard `pip install -e .` command. SQL Server, DB2, and Sybase use `pyodbc`, which is included automatically.

Installing the `pyodbc` package is not enough on its own — each database also needs its matching **ODBC driver installed at the OS level** (this is a Windows/system install, not something `pip` can provide):

| Connector | Default port | Default ODBC driver name | Driver source |
|---|---|---|---|
| Oracle | 1521 | n/a (`oracledb`, no ODBC driver needed) | included via `oracledb` |
| SQL Server | 1433 | `ODBC Driver 17 for SQL Server` | Microsoft ODBC Driver for SQL Server |
| DB2 | 50000 | `IBM DB2 ODBC DRIVER` | IBM Data Server Driver / Db2 client |
| Sybase (SAP ASE) | 5000 | `Adaptive Server Enterprise` | SAP ASE ODBC driver |

The driver name shown above is just the default pre-filled in the connection dialog; it can be overridden per connection if your installed driver is registered under a different name (check ODBC Data Source Administrator on Windows to see what's actually registered).

Anomaly explanations are optional and need a locally running [Ollama](https://ollama.com) server (default `http://localhost:11434`, default model `qwen2.5:7b`, both configurable in settings) — the "Explain with AI" button just won't do anything useful without it, everything else in the app works fine either way. No data leaves the machine for this: the request goes to `localhost`, not a cloud API.

## Run

The app entry point starts the NiceGUI web UI.

```powershell
python -m dqtool.app
```

Or:

```powershell
dqtool
```

Then open `http://127.0.0.1:8080` in a browser.

## Development

```powershell
pip install -e .[dev]
pytest
ruff check .
```

## Workspace, projects, and accounts

The app connects to a single shared **workspace folder**. Each project is a subfolder inside it:

```
Workspace/
  dqtool_workspace.sqlite   <- shared accounts, projects, project access
  ProjectA/
    dqtool_project.sqlite
    exports/
    results/
    uploads/                <- CSV files uploaded through the browser
  ProjectB/
    ...
```

- Accounts are shared across all projects and stored in `dqtool_workspace.sqlite` (username + salted password hash, PBKDF2-SHA256).
- A fresh workspace is seeded with an `admin` / `admin` account; change the password after first login (the app shows a warning banner while the default password is still active).

Roles work on two levels:

- **Workspace Admin** (workspace-wide): creates accounts and projects, assigns project access, and can open every project as admin.
- **Project Admin** (per project): can manage the members of that project (add existing accounts, change roles, remove) and edit or delete any item in it.
- **Project User** (per project): can open the project and manage their own items.

A user only sees and can open the projects where they have a membership; the same account can be admin in one project and user in another. Existing project subfolders (containing `dqtool_project.sqlite`) are auto-registered when the workspace is opened. The **Accounts tab** (Workspace Admin only) manages workspace-wide accounts and, separately, per-project membership and roles.

## Connections

A connection stores how to reach one data source: Oracle, SQL Server, DB2, Sybase, or a CSV file. Database connections store host, port, optional database name, ODBC driver (for SQL Server/DB2/Sybase), and username in the connection record; the password is saved locally per user (see **Secrets storage** below), never in the shared project database.

Each connection has a visibility setting (`private`, `shared`, or `shared_specific`) so it can be scoped to its owner, the whole project, or a specific list of users.

### CSV files: browse vs. upload

The CSV connection dialog offers two ways to pick a file, and both are scoped to that project's own `uploads/` folder rather than the whole server disk:

- **Browse CSV** opens a folder browser confined to `<project>/uploads/` and its subfolders — it cannot navigate above that folder (no drive selector, and "up one level" stops at the folder itself). It only shows files that have already been uploaded to this project.
- **Upload** lets a user pick a file from their own computer through the browser. The file is transferred to the server and saved under `<project>/uploads/<filename>.csv`, keeping its original name. If a file with that name already exists there, a confirmation dialog appears — **Overwrite** replaces the existing file with the new upload, **Cancel** discards the upload and leaves the existing file untouched. This is the option remote users need when their CSV isn't already reachable from the server's filesystem. Only `.csv` files are accepted, and uploads are capped at 200 MB.

In short: **Upload** is how a new file gets into the project; **Browse CSV** is how you pick among files already there.

## Rules, rule groups, and running rules

Rules are built from a fixed set of check types, each with its own setup form:

- **Not Null** — fails rows where a field is database-null
- **Unique** — fails rows whose field or field combination occurs more than once
- **Duplicate** — returns every row sharing values in the selected fields
- **Row Count** — checks the source's row count against a min/max range
- **Value Range** — fails numeric values outside a min/max
- **Regex** — fails text not matching a pattern (not supported on SQL Server or Sybase sources)
- **Length** — checks text length against a min/max
- **Allowed Values** — fails values outside a fixed list
- **Date Validity** — fails values that aren't a valid date (not supported on DB2 or Sybase sources; use custom SQL instead)
- **Custom SQL Failed Rows** — every row a query returns counts as a failure
- **Custom SQL Threshold** — a query returns one `value` metric, compared against a threshold
- **Custom SQL (Whole Connection)** — SQL runs against an entire connection rather than one table/file, so it can join across tables; on CSV connections every file is exposed as a view named after the file
- **Referential Integrity** — fails source rows whose key doesn't exist in a target source
- **Keyed Comparison** — joins source and target by key and fails rows where compared fields differ

**Rule groups** nest rules and subgroups into a tree; running a group runs every rule nested under it (including subgroups) in one batch.

The **Rules tab** overview shows every rule and group as a single indented tree. Each rule row has a checkbox, independent of rule groups — check any combination of rules across the tree and click **Run selected** to execute exactly that set in one batch, with a confirmation dialog and a combined pass/fail/error summary. This is separate from the single-item **Run** button (which runs whichever rule or group is currently selected) and from running a whole group.

Every run — manual, batch, group, or scheduled — lands in the **Results tab**: per-rule run history, pass/fail/error counts, a trend chart, and failed-row previews you can export to CSV.

## Schedules

The **Schedules tab** runs a single rule or an entire rule group automatically, without anyone opening the app:

- **Cadence** is one of three presets: hourly (every N hours), daily (once a day at a chosen HH:MM), or weekly (once a week on a chosen day and HH:MM). Daily and weekly times use the Europe/Brussels time zone.
- The scheduler is a background loop inside the DQTool server process, checked every minute against every project in the workspace — **it only runs while `python -m dqtool.app` (or the packaged `dqtool` entry point) is actually running.** It is not a Windows service and does not survive the app being closed; if you need it to run unattended, keep the process running (e.g., as a scheduled console session on a server) rather than relying on someone's laptop being open.
- Scheduled runs are recorded exactly like manual runs (visible in the Results tab), tagged with `executed_by = "scheduler"` so they're distinguishable from a person's manual run.
- **Run now** on a selected schedule triggers its target immediately, tagged with your own username instead of `"scheduler"`, and does not change the schedule's own next-run time — it's for testing a schedule without disturbing its cadence.
- Deleting a schedule does not delete the underlying rule or group; it only stops the automatic run.
- If a scheduled rule or group is deleted after the schedule was created, the next scheduled run records status `error` rather than silently doing nothing, so it's visible in the Schedules tab.

## Source preview and anomaly detection

The **Preview tab** shows the first rows of any connection's file or table — a quick sanity check before building rules against it.

The **Anomalies tab** goes further: pick a connection and a file/table, run a check, and it profiles the source (row count, per-column null rate, distinct count, mean, etc.) and compares it against the previous snapshot for that same source to flag drift — row count jumps, null spikes, vanished columns, shifted averages. Each run adds a new snapshot, so trend charts (null rate by column, row count over time) build up as checks accumulate. **Explain with AI** sends the profile and drift findings (not the raw data) to a locally running Ollama model and returns a plain-language summary — see the Ollama note under **Install**.

## Central server for multiple users

Run the app on one PC and let users connect with their browsers:

```powershell
$env:DQTOOL_HOST = "0.0.0.0"   # listen on the network (default: 127.0.0.1)
$env:DQTOOL_PORT = "8080"      # optional
python -m dqtool.app
```

Allow inbound TCP 8080 in Windows Firewall, then users browse to `http://<server>:8080` and sign in. Each browser gets its own session; the last-opened project is remembered per user.

Two different pickers behave differently for remote users, worth knowing before you rely on either:

- The **workspace folder picker** (on the login page, used once to point the app at a workspace) browses the whole filesystem of the machine *running the app* — a remote user sees the server's disk, not their own.
- The **CSV file picker** (Browse CSV, in a connection's setup) is scoped to that project's own `uploads/` folder rather than the whole server disk (see **CSV files: browse vs. upload** above) — a remote user gets their own file in via **Upload**, not by browsing.

To serve HTTPS directly (recommended when exposed on the network), point the app at a certificate and key:

```powershell
$env:DQTOOL_SSL_CERTFILE = "C:\certs\dqtool.crt"
$env:DQTOOL_SSL_KEYFILE = "C:\certs\dqtool.key"
```

## Secrets storage

Database connection passwords (Oracle, SQL Server, DB2, Sybase — not just Oracle, despite the internal `oracle_passwords` key name) are stored locally per Windows user in a small JSON file under `%LOCALAPPDATA%\DQTool\secrets.json`, in plaintext, for the MVP. The storage service (`dqtool.services.project`) is isolated behind `save_connection_secret`/`get_connection_secret` so it can be swapped for Windows Credential Manager or another OS keychain later without touching the rest of the app. Login passwords are separate: those are salted PBKDF2-SHA256 hashes in `dqtool_workspace.sqlite`, never plaintext.

## Notes

- The app remembers the last opened workspace folder per Windows user; sign-in is asked on each start.
- A project folder is initialized automatically the first time it is opened.
- Large failed result sets are previewed in the app and can be exported to CSV.

## Architecture

See [docs/architecture.md](docs/architecture.md).
