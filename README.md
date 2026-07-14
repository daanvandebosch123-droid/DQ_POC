# DQTool

DQTool is a Windows-first Python application for defining and running data quality checks against Oracle and CSV datasets inside a shared team project.

## Version 1 scope

- NiceGUI web UI with dashboard, connections, rules, results, and previews
- Shared project metadata stored in a SQLite file inside a shared folder
- `Admin` and `User` roles with item visibility and ownership
- Oracle and CSV connectors first, with room for future connectors
- Built-in rules plus custom SQL rules
- Failed-row previews in the app and export to CSV
- JSON import/export for rules, datasets, and connections

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

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
  ProjectB/
    ...
```

- Accounts are shared across all projects and stored in `dqtool_workspace.sqlite` (username + salted password hash).
- A fresh workspace is seeded with an `admin` / `admin` account; change the password after first login.

Roles work on two levels:

- **Workspace Admin** (workspace-wide): creates accounts and projects, assigns project access, and can open every project as admin.
- **Project Admin** (per project): can manage the members of that project (add existing accounts, change roles, remove) and edit or delete any item in it.
- **Project User** (per project): can open the project and manage their own items.

A user only sees and can open the projects where they have a membership; the same account can be admin in one project and user in another. Existing project subfolders (containing `dqtool_project.sqlite`) are auto-registered when the workspace is opened.

## Central server for multiple users

Run the app on one PC and let users connect with their browsers:

```powershell
$env:DQTOOL_HOST = "0.0.0.0"   # listen on the network (default: 127.0.0.1)
$env:DQTOOL_PORT = "8080"      # optional
python -m dqtool.app
```

Allow inbound TCP 8080 in Windows Firewall, then users browse to `http://<server>:8080` and sign in. Each browser gets its own session; the last-opened project is remembered per user. All folder/file pickers browse the server's filesystem in the browser, so remote users can use them too.

To serve HTTPS directly (recommended when exposed on the network), point the app at a certificate and key:

```powershell
$env:DQTOOL_SSL_CERTFILE = "C:\certs\dqtool.crt"
$env:DQTOOL_SSL_KEYFILE = "C:\certs\dqtool.key"
```

## Notes

- The app remembers the last opened workspace folder per Windows user; sign-in is asked on each start.
- A project folder is initialized automatically the first time it is opened.
- Oracle passwords are stored locally per user in a small JSON file for the MVP. The storage service is isolated so Windows Credential Manager can replace it later.
- Large failed result sets are previewed in the app and can be exported to CSV.

## Architecture

See [docs/architecture.md](docs/architecture.md).
