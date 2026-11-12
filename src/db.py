from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
DEFAULT_DB_PATH = PROJECT_ROOT / "db" / "hookups.sqlite3"
SCHEMA_VERSION = 4
REBUILD_TABLES = ("scores", "contacts", "drafts", "runs", "http_cache", "listings")
COPY_COLUMNS = {
    "listings": (
        "id", "source", "dedupe_key", "source_id", "canonical_url", "title",
        "price_text", "text_excerpt", "thumb_url", "posted_at", "first_seen_at",
        "last_seen_at", "locality", "distance_miles", "tags_json", "review_status",
    ),
    "scores": (
        "id", "listing_id", "scored_at", "score", "geographic_points", "recency_points",
        "preference_points", "intent_points", "completeness_points", "reasons_json",
    ),
    "contacts": ("id", "listing_id", "contacted_at", "method", "notes", "created_at"),
    "drafts": ("id", "listing_id", "body", "status", "created_at", "updated_at"),
    "runs": (
        "id", "source", "started_at", "finished_at", "status", "listings_seen",
        "listings_added", "listings_updated", "error_summary",
    ),
    "http_cache": (
        "source", "url", "fetched_at", "status_code", "final_url", "headers_json", "body",
        "error_summary",
    ),
}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _execute_schema(connection: sqlite3.Connection, schema: str) -> None:
    pending = []
    for line in schema.splitlines():
        pending.append(line)
        statement = "\n".join(pending)
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            pending.clear()
    if "\n".join(pending).strip():
        raise sqlite3.OperationalError("Incomplete SQL statement in schema.sql")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _upgrade_source_checks(connection: sqlite3.Connection, schema: str) -> None:
    if _table_exists(connection, "listings"):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(listings)")}
        if "price_text" not in columns:
            connection.execute("ALTER TABLE listings ADD COLUMN price_text TEXT")
        if "thumb_url" not in columns:
            connection.execute("ALTER TABLE listings ADD COLUMN thumb_url TEXT")
    for (index,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        connection.execute(f"DROP INDEX IF EXISTS {_quote_identifier(index)}")
    renamed = []
    for table in REBUILD_TABLES:
        if _table_exists(connection, table):
            backup = f"{table}__schema2_backup"
            if _table_exists(connection, backup):
                raise RuntimeError(f"Migration backup table already exists: {backup}")
            connection.execute(
                f"ALTER TABLE {_quote_identifier(table)} RENAME TO {_quote_identifier(backup)}"
            )
            renamed.append((table, backup))
    _execute_schema(connection, schema)
    for table, backup in renamed:
        columns = ", ".join(_quote_identifier(column) for column in COPY_COLUMNS[table])
        connection.execute(
            f"INSERT INTO {_quote_identifier(table)} ({columns}) "
            f"SELECT {columns} FROM {_quote_identifier(backup)}"
        )
    for table in REBUILD_TABLES:
        backup = f"{table}__schema2_backup"
        if _table_exists(connection, backup):
            connection.execute(f"DROP TABLE {_quote_identifier(backup)}")


def migrate(db_path: str | Path | None = None) -> None:
    path = str(DEFAULT_DB_PATH if db_path is None else db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection = sqlite3.connect(path, timeout=30)
    legacy_upgrade = False
    try:
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than supported version {SCHEMA_VERSION}"
            )
        legacy_upgrade = current_version in (1, 2)
        if current_version == 0:
            legacy_upgrade = any(_table_exists(connection, table) for table in REBUILD_TABLES)
        if legacy_upgrade:
            connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if legacy_upgrade:
                _upgrade_source_checks(connection, schema)
            else:
                _execute_schema(connection, schema)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"Foreign-key violations after migration: {violations[:3]}")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        if legacy_upgrade:
            connection.execute("PRAGMA foreign_keys = ON")
    finally:
        connection.close()
