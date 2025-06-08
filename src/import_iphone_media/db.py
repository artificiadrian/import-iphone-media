import sqlite3
from datetime import datetime
from pathlib import Path, PurePosixPath

_init_media_db_sql = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS media (
    afc_path TEXT NOT NULL,
    st_size INTEGER NOT NULL,
    st_mtime DATETIME NOT NULL,
    synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(afc_path, st_size, st_mtime)
);
"""


class MediaDatabase:
    """A class to manage a SQLite database for storing information on which iPhone media files have been imported."""

    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_init_media_db_sql)

    def try_insert(self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime):
        """Insert media file information into the database if it does not already exist.
        Args:
            afc_path (PurePosixPath): The file path of the media file on the iPhone.
            st_size (int): The size of the media file in bytes.
            st_mtime (datetime): The last modification time of the media file.

        Returns:
            bool: True if the media file was inserted, False if it already exists."""

        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
                (str(afc_path), st_size, st_mtime.isoformat()),
            )

            return cursor.rowcount == 1

    def close(self):
        self.conn.close()
