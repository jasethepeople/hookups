from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
DEFAULT_DB_PATH = PROJECT_ROOT / "db" / "hookups.sqlite3"
SCHEMA_VERSION = 2


def migrate(db_path: str | Path | None = None) -> None:
    path = str(DEFAULT_DB_PATH if db_path is None else db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than supported version {SCHEMA_VERSION}"
            )
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + schema)
            listing_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(listings)")
            }
            if "price_text" not in listing_columns:
                connection.execute("ALTER TABLE listings ADD COLUMN price_text TEXT")
            if "thumb_url" not in listing_columns:
                connection.execute("ALTER TABLE listings ADD COLUMN thumb_url TEXT")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise
    finally:
        connection.close()
