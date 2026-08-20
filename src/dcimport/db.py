import sqlite3
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 3

_init_media_db_sql = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS media (
    afc_path TEXT NOT NULL,
    st_size INTEGER NOT NULL,
    st_mtime DATETIME NOT NULL,
    synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(afc_path, st_size, st_mtime)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_imports (
    afc_path TEXT NOT NULL,
    st_size INTEGER NOT NULL,
    st_mtime DATETIME NOT NULL,
    local_path TEXT NOT NULL,
    local_size INTEGER NOT NULL,
    UNIQUE(afc_path, st_size, st_mtime)
);
"""


class LegacyTimezoneMigrationError(ValueError):
    """A legacy database needs the timezone used when its imports were recorded."""

    def __init__(self, detail: str | None = None):
        msg = detail or (
            "This media database has legacy timezone-less timestamps. Re-run with"
            " --legacy-timezone set to the IANA timezone used for its imports."
        )
        super().__init__(msg)


class MediaDatabase:
    """SQLite database tracking which device media files have been imported,
    plus per-library settings (e.g. the filename layout).
    A file is identified by its device path, size and modification time."""

    def __init__(
        self,
        db_path: Path,
        legacy_timezone: ZoneInfo | None = None,
        legacy_fold: int | None = None,
    ):
        self.conn = sqlite3.connect(db_path)

        try:
            self.conn.executescript(_init_media_db_sql)
            self._migrate_mtimes_to_epoch(legacy_timezone, legacy_fold)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()
        except BaseException:
            self.conn.close()
            raise

    def _migrate_mtimes_to_epoch(
        self,
        legacy_timezone: ZoneInfo | None,
        legacy_fold: int | None,
    ):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]

        if version >= 2:
            return

        rows = self.conn.execute("SELECT rowid, st_mtime FROM media").fetchall()
        legacy_rows = [row for row in rows if isinstance(row[1], str)]

        if legacy_rows and legacy_timezone is None:
            raise LegacyTimezoneMigrationError()

        for rowid, raw in legacy_rows:
            parsed = datetime.fromisoformat(raw)
            timestamp = _legacy_timestamp(parsed, legacy_timezone, legacy_fold)
            self.conn.execute(
                "UPDATE media SET st_mtime = ? WHERE rowid = ?",
                (timestamp, rowid),
            )

    def contains(self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime):
        """Return whether this exact file (path, size, mtime) was already imported."""

        cursor = self.conn.execute(
            "SELECT 1 FROM media WHERE afc_path = ? AND st_size = ? AND st_mtime = ? LIMIT 1",
            (str(afc_path), st_size, st_mtime.timestamp()),
        )

        return cursor.fetchone() is not None

    def begin_import(
        self,
        afc_path: PurePosixPath,
        st_size: int,
        st_mtime: datetime,
        local_path: Path,
        local_size: int,
    ):
        """Durably record a target before placing the completed file there."""

        with self.conn:
            self.conn.execute(
                "INSERT INTO pending_imports"
                " (afc_path, st_size, st_mtime, local_path, local_size)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(afc_path, st_size, st_mtime) DO UPDATE SET"
                " local_path = excluded.local_path,"
                " local_size = excluded.local_size",
                (
                    str(afc_path),
                    st_size,
                    st_mtime.timestamp(),
                    str(local_path.absolute()),
                    local_size,
                ),
            )

    def complete_import(
        self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime
    ):
        """Atomically mark a pending import complete and remove its recovery record."""

        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
                (str(afc_path), st_size, st_mtime.timestamp()),
            )
            self.conn.execute(
                "DELETE FROM pending_imports"
                " WHERE afc_path = ? AND st_size = ? AND st_mtime = ?",
                (str(afc_path), st_size, st_mtime.timestamp()),
            )

    def reconcile_pending_imports(self):
        """Complete pending imports whose final file exists; discard absent targets."""

        pending = self.conn.execute(
            "SELECT afc_path, st_size, st_mtime, local_path, local_size"
            " FROM pending_imports"
        )

        with self.conn:
            for (
                afc_path,
                st_size,
                st_mtime,
                local_path,
                local_size,
            ) in pending:
                try:
                    path = Path(local_path)
                    metadata = path.lstat()
                    is_complete = (
                        stat.S_ISREG(metadata.st_mode)
                        and metadata.st_size == local_size
                    )
                except FileNotFoundError:
                    is_complete = False

                if is_complete:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO media (afc_path, st_size, st_mtime)"
                        " VALUES (?, ?, ?)",
                        (afc_path, st_size, st_mtime),
                    )

                self.conn.execute(
                    "DELETE FROM pending_imports"
                    " WHERE afc_path = ? AND st_size = ? AND st_mtime = ?",
                    (afc_path, st_size, st_mtime),
                )

    def get_setting(self, key: str):
        """Return the stored value for `key`, or None if unset."""

        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()

        return row[0] if row else None

    def set_setting(self, key: str, value: str):
        """Store `value` under `key`, overwriting any previous value."""

        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self):
        self.conn.close()


def _legacy_timestamp(
    parsed: datetime,
    timezone: ZoneInfo | None,
    legacy_fold: int | None,
) -> float:
    if parsed.tzinfo is not None:
        return parsed.timestamp()

    if timezone is None:
        raise LegacyTimezoneMigrationError()

    timestamps = []

    for fold in (0, 1):
        timestamp = parsed.replace(tzinfo=timezone, fold=fold).timestamp()
        round_trip = datetime.fromtimestamp(timestamp, timezone).replace(tzinfo=None)

        if round_trip == parsed and timestamp not in timestamps:
            timestamps.append(timestamp)

    if not timestamps:
        msg = f"Legacy timestamp {parsed.isoformat()} does not exist in {timezone.key}."
        raise LegacyTimezoneMigrationError(msg)

    if len(timestamps) == 1:
        return timestamps[0]

    if legacy_fold is None:
        msg = (
            f"Legacy timestamp {parsed.isoformat()} is ambiguous in {timezone.key}."
            " Re-run with --legacy-fold earlier or --legacy-fold later."
        )
        raise LegacyTimezoneMigrationError(msg)

    return timestamps[legacy_fold]
