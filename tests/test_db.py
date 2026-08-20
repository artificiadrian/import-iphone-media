import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from dcimport.db import MediaDatabase

PATH = PurePosixPath("/DCIM/100APPLE/IMG_0001.JPG")
MTIME = datetime(2024, 1, 2, 3, 4, 5)
_TZSET = cast("Callable[[], None] | None", vars(time).get("tzset"))


def _complete_import(db: MediaDatabase, target: Path, size: int, mtime: datetime):
    db.begin_import(PATH, size, mtime, target, size)
    db.complete_import(PATH, size, mtime)


def test_contains_false_before_completed_import(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")

    assert not db.contains(PATH, 3, MTIME)


def test_contains_true_after_completed_import(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    _complete_import(db, tmp_path / "IMG_0001.JPG", 3, MTIME)

    assert db.contains(PATH, 3, MTIME)


def test_different_size_is_not_contained(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    _complete_import(db, tmp_path / "IMG_0001.JPG", 3, MTIME)

    assert not db.contains(PATH, 4, MTIME)


def test_get_setting_returns_none_when_unset(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")

    assert db.get_setting("layout") is None


def test_setting_roundtrip_and_persistence(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    db.set_setting("layout", "{name}")
    db.close()

    reopened = open_db(tmp_path / "media.db")

    assert reopened.get_setting("layout") == "{name}"


def test_set_setting_overwrites(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    db.set_setting("layout", "a")
    db.set_setting("layout", "b")

    assert db.get_setting("layout") == "b"


def test_upgrades_v0_database_preserving_records(tmp_path):
    # a database created by version 0.1.x: media table only, no settings, user_version 0
    conn = sqlite3.connect(tmp_path / "media.db")
    conn.executescript(
        "CREATE TABLE media (afc_path TEXT NOT NULL, st_size INTEGER NOT NULL,"
        " st_mtime DATETIME NOT NULL, synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        " UNIQUE(afc_path, st_size, st_mtime));"
    )
    conn.execute(
        "INSERT INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
        (str(PATH), 3, MTIME.isoformat()),
    )
    conn.commit()
    conn.close()

    db = MediaDatabase(tmp_path / "media.db", legacy_timezone=ZoneInfo("UTC"))

    epoch = MTIME.replace(tzinfo=ZoneInfo("UTC")).timestamp()
    assert db.contains(PATH, 3, datetime.fromtimestamp(epoch))
    assert db.get_setting("layout") is None
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    db.close()


def test_legacy_database_requires_original_timezone(tmp_path):
    db_path = tmp_path / "media.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE media (afc_path TEXT NOT NULL, st_size INTEGER NOT NULL,"
        " st_mtime DATETIME NOT NULL, synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        " UNIQUE(afc_path, st_size, st_mtime))"
    )
    conn.execute(
        "INSERT INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
        (str(PATH), 3, MTIME.isoformat()),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="legacy timezone"):
        MediaDatabase(db_path)


def test_legacy_database_migration_uses_original_timezone(tmp_path):
    db_path = tmp_path / "media.db"
    epoch = 1_700_000_000
    berlin = ZoneInfo("Europe/Berlin")
    legacy_mtime = datetime.fromtimestamp(epoch, berlin).replace(tzinfo=None)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE media (afc_path TEXT NOT NULL, st_size INTEGER NOT NULL,"
        " st_mtime DATETIME NOT NULL, synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        " UNIQUE(afc_path, st_size, st_mtime))"
    )
    conn.execute(
        "INSERT INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
        (str(PATH), 3, legacy_mtime.isoformat()),
    )
    conn.commit()
    conn.close()
    db = MediaDatabase(db_path, legacy_timezone=berlin, legacy_fold=1)
    try:
        assert db.contains(PATH, 3, datetime.fromtimestamp(epoch, UTC))
    finally:
        db.close()


def test_legacy_database_migration_requires_ambiguous_dst_fold(tmp_path):
    db_path = tmp_path / "media.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE media (afc_path TEXT NOT NULL, st_size INTEGER NOT NULL,"
        " st_mtime DATETIME NOT NULL, synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        " UNIQUE(afc_path, st_size, st_mtime))"
    )
    conn.execute(
        "INSERT INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
        (str(PATH), 3, "2024-11-03T01:30:00"),
    )
    conn.commit()
    conn.close()
    new_york = ZoneInfo("America/New_York")

    with pytest.raises(ValueError, match="ambiguous"):
        MediaDatabase(db_path, legacy_timezone=new_york)

    db = MediaDatabase(db_path, legacy_timezone=new_york, legacy_fold=0)
    try:
        first = datetime(2024, 11, 3, 1, 30, tzinfo=new_york, fold=0)
        second = datetime(2024, 11, 3, 1, 30, tzinfo=new_york, fold=1)
        assert db.contains(PATH, 3, first)
        assert not db.contains(PATH, 3, second)
    finally:
        db.close()


def test_legacy_database_migration_rejects_nonexistent_local_time(tmp_path):
    db_path = tmp_path / "media.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE media (afc_path TEXT NOT NULL, st_size INTEGER NOT NULL,"
        " st_mtime DATETIME NOT NULL, synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        " UNIQUE(afc_path, st_size, st_mtime))"
    )
    conn.execute(
        "INSERT INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
        (str(PATH), 3, "2024-03-31T02:30:00"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="does not exist"):
        MediaDatabase(db_path, legacy_timezone=ZoneInfo("Europe/Berlin"))


@pytest.mark.skipif(_TZSET is None, reason="time.tzset is POSIX-only")
def test_dedup_survives_host_timezone_change(tmp_path, open_db, monkeypatch):
    assert _TZSET is not None

    # pymobiledevice3 derives st_mtime via datetime.fromtimestamp (naive local time),
    # so the same device file yields a different wall-clock in a different timezone.
    # Dedup must key on the underlying instant, not the local rendering.
    epoch = 1_700_000_000

    monkeypatch.setenv("TZ", "Europe/Berlin")
    _TZSET()
    try:
        db = open_db(tmp_path / "media.db")
        _complete_import(
            db, tmp_path / "IMG_0001.JPG", 100, datetime.fromtimestamp(epoch)
        )

        monkeypatch.setenv("TZ", "America/New_York")
        _TZSET()

        assert db.contains(PATH, 100, datetime.fromtimestamp(epoch))
    finally:
        monkeypatch.undo()
        _TZSET()


def test_completed_imports_persist_across_reopen(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    _complete_import(db, tmp_path / "IMG_0001.JPG", 3, MTIME)
    db.close()

    reopened = open_db(tmp_path / "media.db")

    assert reopened.contains(PATH, 3, MTIME)


def test_reconciles_completed_pending_import(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    target = tmp_path / "IMG_0001.JPG"
    target.write_bytes(b"jpg")
    db.begin_import(
        PATH,
        3,
        MTIME,
        target,
        3,
    )

    db.reconcile_pending_imports()

    assert db.contains(PATH, 3, MTIME)


def test_does_not_reconcile_file_with_different_size(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    target = tmp_path / "IMG_0001.JPG"
    target.write_bytes(b"different")
    db.begin_import(
        PATH,
        3,
        MTIME,
        target,
        3,
    )

    db.reconcile_pending_imports()

    assert not db.contains(PATH, 3, MTIME)


def test_does_not_reconcile_symlink(tmp_path, open_db):
    db = open_db(tmp_path / "media.db")
    outside = tmp_path / "outside.JPG"
    outside.write_bytes(b"jpg")
    target = tmp_path / "IMG_0001.JPG"
    target.symlink_to(outside)
    db.begin_import(
        PATH,
        3,
        MTIME,
        target,
        3,
    )

    db.reconcile_pending_imports()

    assert not db.contains(PATH, 3, MTIME)
