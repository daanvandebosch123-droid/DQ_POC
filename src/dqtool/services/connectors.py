from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from dqtool.models.entities import Connection, ConnectionType, Dataset, DatasetType
from dqtool.services.project import get_connection_secret

try:
    import oracledb
except Exception:  # pragma: no cover - allows UI boot without Oracle libs
    oracledb = None

try:
    import pyodbc
except Exception:  # pragma: no cover - allows UI boot without ODBC libs
    pyodbc = None


@dataclass(frozen=True, slots=True)
class OdbcSettings:
    """Everything that differs between the ODBC-based database types."""

    default_driver: str
    default_port: int
    template: str  # connection-string template; placeholders: driver, host, port, database, username, password


ODBC_SETTINGS: dict[ConnectionType, OdbcSettings] = {
    ConnectionType.SQLSERVER: OdbcSettings(
        default_driver="ODBC Driver 17 for SQL Server",
        default_port=1433,
        template=(
            "DRIVER={{{driver}}};SERVER={host},{port};DATABASE={database};"
            "UID={username};PWD={password};TrustServerCertificate=yes;"
        ),
    ),
    ConnectionType.DB2: OdbcSettings(
        default_driver="IBM DB2 ODBC DRIVER",
        default_port=50000,
        template=(
            "DRIVER={{{driver}}};DATABASE={database};HOSTNAME={host};PORT={port};"
            "PROTOCOL=TCPIP;UID={username};PWD={password};"
        ),
    ),
    ConnectionType.SYBASE: OdbcSettings(
        default_driver="Adaptive Server Enterprise",
        default_port=5000,
        template="DRIVER={{{driver}}};NA={host},{port};UID={username};PWD={password};DB={database};",
    ),
}


class ConnectorService:
    def __init__(self) -> None:
        self._encoding_cache: dict[tuple[str, int, int], str] = {}

    def preview_dataset(self, dataset: Dataset, connection_lookup: dict[int, Connection]) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
        if dataset.dataset_type in {DatasetType.CSV_FILE, DatasetType.CSV_FOLDER_FILE, DatasetType.APP_JOIN}:
            return self._preview_local(dataset, connection_lookup)
        return self._preview_database(dataset, connection_lookup)

    def preview_rule_source(self, source_config: dict[str, Any], connection_lookup: dict[int, Connection]) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
        connection = self._rule_connection(source_config, connection_lookup)
        if connection.connection_type == ConnectionType.CSV:
            return self._preview_csv_source(source_config, connection)
        return self._preview_database_source(source_config, connection)

    def list_dataset_columns(self, dataset: Dataset, connection_lookup: dict[int, Connection]) -> list[str]:
        if dataset.dataset_type in {DatasetType.CSV_FILE, DatasetType.CSV_FOLDER_FILE, DatasetType.APP_JOIN}:
            con = duckdb.connect()
            try:
                relation = self._build_local_relation(con, dataset, connection_lookup)
                return [column[0] for column in relation.description]
            finally:
                con.close()

        connection = connection_lookup[dataset.connection_id or 0]
        db_conn = self.connect_database(connection)
        sql = dataset.config["sql"] if dataset.dataset_type == DatasetType.ORACLE_SQL else f"SELECT * FROM {dataset.config['table_name']}"
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(self.limited_sql(sql, 0, self.database_dialect(connection)))
                return [column[0] for column in cursor.description]
        finally:
            db_conn.close()

    def list_connection_targets(self, connection: Connection) -> list[str]:
        if connection.connection_type == ConnectionType.CSV:
            selected_file = self.csv_connection_file(connection)
            if selected_file is not None:
                return [selected_file.name]
            base_path = Path(connection.config.get("base_path", ""))
            if not base_path.exists():
                return []
            return sorted(str(path.relative_to(base_path)).replace("\\", "/") for path in base_path.rglob("*.csv"))

        queries = {
            ConnectionType.ORACLE: (
                "SELECT owner || '.' || object_name FROM all_objects "
                "WHERE object_type IN ('TABLE', 'VIEW') ORDER BY owner, object_name"
            ),
            ConnectionType.SQLSERVER: (
                "SELECT TABLE_SCHEMA + '.' + TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW') ORDER BY TABLE_SCHEMA, TABLE_NAME"
            ),
            ConnectionType.DB2: (
                "SELECT TRIM(TABSCHEMA) || '.' || TRIM(TABNAME) FROM SYSCAT.TABLES "
                "WHERE TYPE IN ('T', 'V') AND TABSCHEMA NOT LIKE 'SYS%' ORDER BY TABSCHEMA, TABNAME"
            ),
            ConnectionType.SYBASE: (
                "SELECT USER_NAME(o.uid) + '.' + o.name FROM sysobjects o "
                "WHERE o.type IN ('U', 'V') ORDER BY USER_NAME(o.uid), o.name"
            ),
        }
        db_conn = self.connect_database(connection)
        try:
            if connection.connection_type != ConnectionType.DB2:
                with db_conn.cursor() as cursor:
                    cursor.execute(queries[connection.connection_type])
                    return [row[0] for row in cursor.fetchall()]

            with db_conn.cursor() as cursor:
                try:
                    cursor.execute(queries[connection.connection_type])
                    return [row[0] for row in cursor.fetchall()]
                except Exception as first_exc:
                    pass

            with db_conn.cursor() as cursor:
                try:
                    cursor.execute(
                        "SELECT RTRIM(CREATOR) || '.' || RTRIM(NAME) FROM SYSIBM.SYSTABLES "
                        "WHERE TYPE IN ('T', 'V') AND CREATOR NOT LIKE 'SYS%' ORDER BY CREATOR, NAME"
                    )
                    return [row[0] for row in cursor.fetchall()]
                except Exception:
                    tables = [
                        f"{row[1]}.{row[2]}"
                        for row in cursor.tables()
                        if row[3] in {"TABLE", "VIEW"}
                    ]
                    if tables:
                        return sorted(set(tables))
                    raise first_exc
        finally:
            db_conn.close()

    def list_rule_source_columns(self, source_config: dict[str, Any], connection_lookup: dict[int, Connection]) -> list[str]:
        connection = self._rule_connection(source_config, connection_lookup)
        if connection.connection_type == ConnectionType.CSV:
            return self._csv_source_columns(source_config, connection)
        return self._database_source_columns(source_config, connection)

    def _preview_local(self, dataset: Dataset, connection_lookup: dict[int, Connection]) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
        con = duckdb.connect()
        try:
            relation = self._build_local_relation(con, dataset, connection_lookup)
            rows = relation.limit(100).fetchall()
            columns = [item[0] for item in relation.description]
            stats = {"row_count": con.execute(f"SELECT COUNT(*) FROM ({relation.sql_query()}) q").fetchone()[0]}
            return columns, [list(row) for row in rows], stats
        finally:
            con.close()

    def _preview_database(self, dataset: Dataset, connection_lookup: dict[int, Connection]) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
        connection = connection_lookup[dataset.connection_id or 0]
        db_conn = self.connect_database(connection)
        sql = dataset.config["sql"] if dataset.dataset_type == DatasetType.ORACLE_SQL else f"SELECT * FROM {dataset.config['table_name']}"
        preview_sql = self.limited_sql(sql, 100, self.database_dialect(connection))
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(preview_sql)
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                cursor.execute(f"SELECT COUNT(*) FROM ({sql}) q")
                stats = {"row_count": cursor.fetchone()[0]}
            return columns, [list(row) for row in rows], stats
        finally:
            db_conn.close()

    def _preview_csv_source(self, source_config: dict[str, Any], connection: Connection) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
        csv_path = self._rule_csv_path(connection, source_config)
        con = duckdb.connect()
        try:
            relation = con.sql(f"SELECT * FROM {self._csv_reader(csv_path)}")
            rows = relation.limit(100).fetchall()
            columns = [item[0] for item in relation.description]
            stats = {"row_count": con.execute(f"SELECT COUNT(*) FROM ({relation.sql_query()}) q").fetchone()[0]}
            return columns, [list(row) for row in rows], stats
        finally:
            con.close()

    def _preview_database_source(self, source_config: dict[str, Any], connection: Connection) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
        db_conn = self.connect_database(connection)
        sql = self.rule_source_sql(source_config)
        preview_sql = self.limited_sql(sql, 100, self.database_dialect(connection))
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(preview_sql)
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                cursor.execute(f"SELECT COUNT(*) FROM ({sql}) q")
                stats = {"row_count": cursor.fetchone()[0]}
            return columns, [list(row) for row in rows], stats
        finally:
            db_conn.close()

    def test_connection(self, connection: Connection) -> tuple[bool, str]:
        try:
            if connection.connection_type == ConnectionType.CSV:
                selected_file = self.csv_connection_file(connection)
                if selected_file is not None:
                    return selected_file.is_file(), f"CSV file {'found' if selected_file.is_file() else 'not found'}: {selected_file}"
                path = Path(connection.config.get("base_path", ""))
                return path.is_dir(), f"Legacy CSV folder {'found' if path.is_dir() else 'not found'}: {path}"
            db_conn = self.connect_database(connection)
            db_conn.close()
            return True, f"{connection.connection_type.value.capitalize()} connection succeeded."
        except Exception as exc:
            return False, str(exc)

    def database_dialect(self, connection: Connection) -> str:
        return connection.connection_type.value

    def connect_database(self, connection: Connection):
        if connection.connection_type == ConnectionType.ORACLE:
            return self._connect_oracle(connection)
        if connection.connection_type in ODBC_SETTINGS:
            return self._connect_odbc(connection)
        raise RuntimeError(f"Unsupported database connection type: {connection.connection_type.value}")

    def limited_sql(self, sql: str, limit: int, dialect: str) -> str:
        """Wrap a query so it returns at most `limit` rows, in the dialect's syntax."""
        if dialect in {"sqlserver", "sybase"}:
            return f"SELECT TOP {int(limit)} * FROM ({sql}) q"
        return f"SELECT * FROM ({sql}) q FETCH FIRST {int(limit)} ROWS ONLY"

    def _database_password(self, connection: Connection, username: str | None) -> str:
        password = get_connection_secret(connection.name, username)
        if not password:
            raise RuntimeError("No saved local password found for this connection.")
        return password

    def _connect_odbc(self, connection: Connection):
        """Shared pyodbc connect for every ODBC-based type; per-type details live in ODBC_SETTINGS."""
        if pyodbc is None:
            raise RuntimeError("The pyodbc package is not installed. Install it with: pip install pyodbc")
        settings = ODBC_SETTINGS[connection.connection_type]
        username = connection.config.get("username")
        connection_string = settings.template.format(
            driver=connection.config.get("driver") or settings.default_driver,
            host=connection.config.get("host"),
            port=connection.config.get("port") or settings.default_port,
            database=connection.config.get("database") or "",
            username=username,
            password=self._database_password(connection, username),
        )
        return pyodbc.connect(connection_string, timeout=10)

    def _connect_oracle(self, connection: Connection):
        if oracledb is None:
            raise RuntimeError("The oracledb package is not available.")
        username = connection.config.get("username")
        password = get_connection_secret(connection.name, username)
        if not password:
            raise RuntimeError("No saved local Oracle password found for this connection.")
        dsn = connection.config.get("dsn")
        if not dsn:
            host = connection.config.get("host")
            port = connection.config.get("port")
            service_name = connection.config.get("service_name")
            if host and port and service_name:
                dsn = f"{host}:{port}/{service_name}"
            else:
                dsn = connection.config.get("tns_alias")
        return oracledb.connect(user=username, password=password, dsn=dsn)

    def _build_local_relation(self, con: duckdb.DuckDBPyConnection, dataset: Dataset, connection_lookup: dict[int, Connection]):
        if dataset.dataset_type in {DatasetType.CSV_FILE, DatasetType.CSV_FOLDER_FILE}:
            csv_path = self._resolve_csv_path(dataset, connection_lookup)
            csv_reader = self._csv_reader(csv_path, dataset.config.get("encoding"))
            return con.sql(f"SELECT * FROM {csv_reader}")
        if dataset.dataset_type == DatasetType.APP_JOIN:
            sources = dataset.config["sources"]
            relation_map: dict[str, str] = {}
            for alias, source in sources.items():
                source_path = Path(source["path"])
                relation_name = f"t_{alias}"
                relation_map[alias] = relation_name
                csv_reader = self._csv_reader(source_path, source.get("encoding"))
                con.sql(f"CREATE OR REPLACE VIEW {relation_name} AS SELECT * FROM {csv_reader}")
            query = dataset.config["sql"]
            return con.sql(query)
        raise RuntimeError(f"Unsupported local dataset type: {dataset.dataset_type}")

    def build_rule_source_relation(self, con: duckdb.DuckDBPyConnection, source_config: dict[str, Any], connection_lookup: dict[int, Connection]):
        connection = self._rule_connection(source_config, connection_lookup)
        if connection.connection_type != ConnectionType.CSV:
            raise RuntimeError("Only CSV connections can be loaded into the local rule engine.")
        csv_path = self._rule_csv_path(connection, source_config)
        return con.sql(f"SELECT * FROM {self._csv_reader(csv_path)}")

    def register_connection_views(self, con: duckdb.DuckDBPyConnection, connection: Connection) -> dict[str, str]:
        """Expose every CSV file of a connection as a DuckDB view named after the file.

        customers.csv becomes the view `customers`; names are sanitized so they stay
        valid SQL identifiers. Returns view name -> file path for reference.
        """
        if connection.connection_type != ConnectionType.CSV:
            raise RuntimeError("Only CSV connections can be loaded into the local rule engine.")
        selected_file = self.csv_connection_file(connection)
        if selected_file is not None:
            files = [selected_file]
        else:
            base_path = Path(str(connection.config.get("base_path") or ""))
            if not base_path.is_dir():
                raise ValueError(f"The CSV folder was not found: {base_path}")
            files = sorted(base_path.rglob("*.csv"))
        if not files:
            raise ValueError("The connection contains no CSV files.")
        registered: dict[str, str] = {}
        for file_path in files:
            view_name = self._view_name_for_file(file_path, registered)
            con.sql(f'CREATE OR REPLACE VIEW "{view_name}" AS SELECT * FROM {self._csv_reader(file_path)}')
            registered[view_name] = str(file_path)
        return registered

    def _view_name_for_file(self, file_path: Path, taken: dict[str, str]) -> str:
        base = re.sub(r"\W+", "_", file_path.stem, flags=re.UNICODE).strip("_") or "csv"
        if base[0].isdigit():
            base = f"t_{base}"
        candidate = base
        index = 2
        while candidate in taken:
            candidate = f"{base}_{index}"
            index += 1
        return candidate

    def rule_source_sql(self, source_config: dict[str, Any]) -> str:
        if source_config.get("source_kind") == "oracle_sql":
            return source_config["source_sql"]
        return f"SELECT * FROM {source_config['source_name']}"

    def _resolve_csv_path(self, dataset: Dataset, connection_lookup: dict[int, Connection]) -> Path:
        if dataset.dataset_type == DatasetType.CSV_FILE:
            return Path(dataset.config["path"])
        connection = connection_lookup[dataset.connection_id or 0]
        return Path(connection.config["base_path"]) / dataset.config["relative_path"]

    def _rule_connection(self, source_config: dict[str, Any], connection_lookup: dict[int, Connection]) -> Connection:
        connection_id = int(source_config.get("source_connection_id") or 0)
        if connection_id not in connection_lookup:
            raise ValueError("Select a valid connection.")
        return connection_lookup[connection_id]

    def _csv_source_columns(self, source_config: dict[str, Any], connection: Connection) -> list[str]:
        csv_path = self._rule_csv_path(connection, source_config)
        con = duckdb.connect()
        try:
            relation = con.sql(f"SELECT * FROM {self._csv_reader(csv_path)} LIMIT 0")
            return [column[0] for column in relation.description]
        finally:
            con.close()

    def csv_connection_file(self, connection: Connection) -> Path | None:
        configured_file = str(connection.config.get("file_path") or "").strip()
        if configured_file:
            return Path(configured_file)

        base_value = str(connection.config.get("base_path") or "").strip()
        if not base_value:
            return None
        base_path = Path(base_value)
        if base_path.is_file():
            return base_path

        filename = connection.name if connection.name.lower().endswith(".csv") else f"{connection.name}.csv"
        inferred_file = base_path / filename
        return inferred_file if inferred_file.is_file() else None

    def _rule_csv_path(self, connection: Connection, source_config: dict[str, Any]) -> Path:
        selected_file = self.csv_connection_file(connection)
        if selected_file is not None:
            return selected_file
        return Path(connection.config["base_path"]) / str(source_config.get("source_name") or "")

    def _database_source_columns(self, source_config: dict[str, Any], connection: Connection) -> list[str]:
        db_conn = self.connect_database(connection)
        sql = self.rule_source_sql(source_config)
        try:
            with db_conn.cursor() as cursor:
                cursor.execute(self.limited_sql(sql, 0, self.database_dialect(connection)))
                return [column[0] for column in cursor.description]
        finally:
            db_conn.close()

    def detect_csv_schema(self, file_path: Path) -> list[dict[str, str]]:
        con = duckdb.connect()
        try:
            relation = con.sql(f"SELECT * FROM {self._csv_reader(file_path)} LIMIT 0")
            return [{"name": name, "type": str(column_type)} for name, column_type, *_ in relation.description]
        finally:
            con.close()

    def _csv_reader(self, file_path: Path, configured_encoding: str | None = None) -> str:
        encoding = configured_encoding
        if not encoding or encoding.lower() == "auto":
            encoding = self._detect_csv_encoding(file_path)
        normalized_encoding = {
            "utf-8-sig": "utf-8",
            "cp1252": "latin-1",
            "windows-1252": "latin-1",
        }.get(encoding.lower(), encoding.lower())
        escaped_path = file_path.as_posix().replace("'", "''")
        escaped_encoding = normalized_encoding.replace("'", "''")
        return f"read_csv_auto('{escaped_path}', encoding='{escaped_encoding}')"

    def _detect_csv_encoding(self, file_path: Path) -> str:
        file_stat = file_path.stat()
        cache_key = (str(file_path.resolve()), file_stat.st_mtime_ns, file_stat.st_size)
        cached = self._encoding_cache.get(cache_key)
        if cached:
            return cached

        with file_path.open("rb") as handle:
            prefix = handle.read(4)
        if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif prefix.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8"
        else:
            try:
                with file_path.open("r", encoding="utf-8") as handle:
                    while handle.read(1024 * 1024):
                        pass
                encoding = "utf-8"
            except UnicodeDecodeError:
                encoding = "latin-1"

        if len(self._encoding_cache) > 256:
            self._encoding_cache.clear()
        self._encoding_cache[cache_key] = encoding
        return encoding
