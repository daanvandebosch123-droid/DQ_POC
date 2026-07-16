# Architecture

## UI stack

- `NiceGUI` for the web UI
- `SQLite` inside the shared project folder for metadata
- `DuckDB` for CSV querying, previews, joins, and cross-source comparison execution
- `oracledb` for Oracle connectivity and Oracle SQL pushdown

## Core layers

- `models.entities`: dataclasses and enums
- `services.project`: project initialization, recent project tracking, local secrets
- `services.storage`: SQLite schema and CRUD helpers
- `services.connectors`: Oracle/CSV connection handling and previews
- `services.rules`: built-in rule metadata and persistence helpers
- `services.execution`: batch rule execution, summaries, preview rows, and exports
- `services.scheduling`: pure cadence math (next-run computation, human-readable descriptions) for the rule scheduler
- `web_app`: NiceGUI shell for project management, connections, rules, execution, results, and the scheduler UI/background loop

## Shared project model

The shared SQLite project holds:

- users
- connections
- datasets
- rules
- run history
- rule results
- schedules (which rule/group, cadence, next/last run)
- sharing metadata
- tags as lightweight fields

## MVP decisions

- Role model is `Admin` and `User`
- Conflict handling records timestamps; UI warns if an item changed since it was loaded
- Passwords are local per user only, never shared in project storage
- Oracle and CSV schemas are auto-detected with manual column JSON overrides supported later
- Built-in rule execution supports row counts, nulls, uniqueness, duplicates, ranges, allowed values, regex, length, date validity, referential integrity, and keyed comparisons
