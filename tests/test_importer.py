import asyncio
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
from typing_extensions import override

from dcimport.db import MediaDatabase
from dcimport.importer import (
    FailedFile,
    LayoutConflictError,
    NewFile,
    execute_import,
    plan_import,
    resolve_layout,
)
from dcimport.layout import DEFAULT_LAYOUT
from tests.fake_source import DEFAULT_MTIME as MTIME
from tests.fake_source import FakeSource, InMemoryDb
from tests.helpers import persistently_fail, run_import, scan


@pytest.fixture
def source():
    return FakeSource()


# --- scan phase: classification ---


def test_ignores_excluded_extensions_without_stat(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.AAE")

    plan = scan(source, tmp_path)

    assert PurePosixPath("/DCIM/100APPLE/IMG_0001.AAE") in plan.ignored
    assert source.stat_calls["/DCIM/100APPLE/IMG_0001.AAE"] == 0


def test_ignores_directories(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")

    plan = scan(source, tmp_path)

    assert PurePosixPath("/DCIM/100APPLE") in plan.ignored


def test_second_run_skips_existing_file(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")

    run_import(source, tmp_path)
    plan = scan(source, tmp_path)

    assert not plan.to_download
    assert [p.afc_path.name for p in plan.existing] == ["IMG_0001.JPG"]


def test_stats_each_file_exactly_once(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")

    scan(source, tmp_path)

    assert source.stat_calls["/DCIM/100APPLE/IMG_0001.JPG"] == 1


# --- download phase ---


def test_downloads_new_file_with_timestamped_name(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)

    results = run_import(source, tmp_path)

    target = tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG"
    new = [r for r in results if isinstance(r, NewFile)]
    assert [f.local_path for f in new] == [target]
    assert target.read_bytes() == b"jpegdata"


def test_sets_local_mtime_to_iphone_mtime(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", mtime=MTIME)

    run_import(source, tmp_path)

    target = tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG"
    assert target.stat().st_mtime == MTIME.timestamp()


def test_interrupted_download_leaves_no_trace(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    persistently_fail(source, "/DCIM/100APPLE/IMG_0001.JPG")

    run_import(source, tmp_path)

    leftovers = [p for p in tmp_path.iterdir() if not p.name.startswith("media.db")]
    assert leftovers == []


def test_persistent_failure_yields_failed_file_and_continues(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")
    source.add("/DCIM/100APPLE/IMG_0002.JPG", data=b"second")
    persistently_fail(source, "/DCIM/100APPLE/IMG_0001.JPG")

    results = run_import(source, tmp_path)

    failed = [r for r in results if isinstance(r, FailedFile)]
    assert [str(f.afc_path) for f in failed] == ["/DCIM/100APPLE/IMG_0001.JPG"]
    assert "usb died" in failed[0].error

    new = [r for r in results if isinstance(r, NewFile)]
    assert [n.afc_path.name for n in new] == ["IMG_0002.JPG"]


def test_failed_file_is_reattempted_on_next_run(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    persistently_fail(source, "/DCIM/100APPLE/IMG_0001.JPG")

    first = run_import(source, tmp_path)
    second = run_import(source, tmp_path)

    assert any(isinstance(r, FailedFile) for r in first)
    assert any(isinstance(r, NewFile) for r in second)
    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG").read_bytes() == b"jpegdata"


def test_transient_download_failure_is_retried(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    source.fail_next_download("/DCIM/100APPLE/IMG_0001.JPG", OSError("usb glitch"))

    results = run_import(source, tmp_path)

    assert any(isinstance(r, NewFile) for r in results)
    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG").read_bytes() == b"jpegdata"


def test_keyboard_interrupt_is_not_retried(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")
    source.fail_next_download("/DCIM/100APPLE/IMG_0001.JPG", KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        run_import(source, tmp_path)

    assert source.download_calls["/DCIM/100APPLE/IMG_0001.JPG"] == 1


def test_keyboard_interrupt_does_not_log_unretrieved_task():
    script = textwrap.dedent(
        """
        import asyncio
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from dcimport.importer import execute_import, plan_import, resolve_layout
        from tests.fake_source import FakeSource, InMemoryDb

        async def run():
            source = FakeSource()
            source.add("/DCIM/100APPLE/IMG_0001.JPG")
            source.fail_next_download("/DCIM/100APPLE/IMG_0001.JPG", KeyboardInterrupt())
            db = InMemoryDb()
            plan = await plan_import(source, db)
            with TemporaryDirectory() as directory:
                async for _ in execute_import(
                    source,
                    db,
                    plan,
                    Path(directory),
                    resolve_layout(db, None, False),
                ):
                    pass

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            pass
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Task exception was never retrieved" not in result.stderr


def test_name_collisions_get_numeric_suffix(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"first", mtime=MTIME)
    source.add("/DCIM/101APPLE/IMG_0001.JPG", data=b"second", mtime=MTIME)

    new = [r for r in run_import(source, tmp_path) if isinstance(r, NewFile)]

    assert sorted(n.local_path.name for n in new) == [
        "2024-01-02_03-04-05_IMG_0001.JPG",
        "2024-01-02_03-04-05_IMG_0001_1.JPG",
    ]
    assert sorted(n.local_path.read_bytes() for n in new) == [b"first", b"second"]


def test_existing_local_file_is_not_overwritten(source, tmp_path):
    (tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG").write_bytes(b"precious")
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"new", mtime=MTIME)

    run_import(source, tmp_path)

    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG").read_bytes() == b"precious"
    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001_1.JPG").read_bytes() == b"new"


def test_dangling_target_symlink_gets_numeric_suffix(source, tmp_path):
    target = tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG"
    target.symlink_to(tmp_path / "missing.JPG")
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"new", mtime=MTIME)

    run_import(source, tmp_path)

    assert target.is_symlink()
    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001_1.JPG").read_bytes() == b"new"


def test_missing_output_directory_is_created(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"x", mtime=MTIME)

    results = run_import(source, tmp_path / "photos" / "iphone")

    assert any(isinstance(r, NewFile) for r in results)
    assert (
        tmp_path / "photos" / "iphone" / "2024-01-02_03-04-05_IMG_0001.JPG"
    ).read_bytes() == b"x"


def test_layout_can_follow_user_created_directory_symlink(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    output_path = tmp_path / "photos"
    outside_path = tmp_path / "outside"
    output_path.mkdir()
    outside_path.mkdir()
    (output_path / "album").symlink_to(outside_path, target_is_directory=True)

    results = run_import(source, output_path, layout="album/{name}")

    assert isinstance(results[0], NewFile)
    assert (outside_path / "IMG_0001.JPG").read_bytes() == b"jpegdata"


def test_download_does_not_follow_temporary_symlink(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    outside = tmp_path / "outside.JPG"
    outside.write_bytes(b"precious")
    temporary = tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG.part"
    temporary.symlink_to(outside)

    results = run_import(source, tmp_path)

    assert any(isinstance(result, NewFile) for result in results)
    assert outside.read_bytes() == b"precious"
    assert temporary.is_symlink()
    assert not (tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG").is_symlink()


def test_target_created_during_download_is_not_overwritten(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    target = tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG"
    original_download = source.download

    async def download(path, output):
        await original_download(path, output)
        target.write_bytes(b"precious")

    source.download = download

    results = run_import(source, tmp_path)

    assert isinstance(results[0], FailedFile)
    assert target.read_bytes() == b"precious"
    assert scan(source, tmp_path).to_download


def test_execute_import_rejects_nonpositive_concurrency(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")

    async def download():
        db = InMemoryDb()
        plan = await plan_import(source, db)
        layout = resolve_layout(db, None, False)
        return [
            result
            async for result in execute_import(
                source, db, plan, tmp_path, layout, concurrency=0
            )
        ]

    with pytest.raises(ValueError, match="concurrency"):
        asyncio.run(asyncio.wait_for(download(), timeout=0.01))


def test_execute_import_rejects_negative_retry_count(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")

    async def download():
        db = InMemoryDb()
        plan = await plan_import(source, db)
        layout = resolve_layout(db, None, False)
        return [
            result
            async for result in execute_import(
                source, db, plan, tmp_path, layout, download_retries=-1
            )
        ]

    with pytest.raises(ValueError, match="download_retries"):
        asyncio.run(download())


class BlockingDownloadSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    @override
    async def download(self, path, target):
        self.download_calls[str(path)] += 1
        self.active_downloads += 1
        self.max_concurrent_downloads = max(
            self.max_concurrent_downloads, self.active_downloads
        )

        if self.active_downloads == 2:
            self.started.set()

        try:
            await self.gate.wait()
            target.write(self.files[str(path)].data)
        finally:
            self.active_downloads -= 1


class RollingDownloadSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.first_gate = asyncio.Event()
        self.third_started = asyncio.Event()

    @override
    async def download(self, path, target):
        self.download_calls[str(path)] += 1

        if path.name == "IMG_0000.JPG":
            await self.first_gate.wait()
        elif path.name == "IMG_0002.JPG":
            self.third_started.set()

        target.write(self.files[str(path)].data)


def test_execute_import_creates_only_one_batch_of_download_tasks(tmp_path):
    source = BlockingDownloadSource()
    for i in range(100):
        source.add(f"/DCIM/100APPLE/IMG_{i:04}.JPG")

    async def download():
        db = InMemoryDb()
        plan = await plan_import(source, db)
        layout = resolve_layout(db, None, False)

        async def collect():
            return [
                result
                async for result in execute_import(
                    source, db, plan, tmp_path, layout, concurrency=2
                )
            ]

        task = asyncio.create_task(collect())
        await source.started.wait()
        pending = len(asyncio.all_tasks()) - 1
        source.gate.set()
        await task
        return pending

    assert asyncio.run(download()) <= 3


def test_execute_import_reuses_available_download_slot(tmp_path):
    source = RollingDownloadSource()
    for i in range(3):
        source.add(f"/DCIM/100APPLE/IMG_{i:04}.JPG")

    async def download():
        db = InMemoryDb()
        plan = await plan_import(source, db)
        layout = resolve_layout(db, None, False)

        async def collect():
            return [
                result
                async for result in execute_import(
                    source, db, plan, tmp_path, layout, concurrency=2
                )
            ]

        task = asyncio.create_task(collect())
        await asyncio.wait_for(source.third_started.wait(), timeout=1)
        source.first_gate.set()
        await task

    asyncio.run(download())


def test_downloads_run_concurrently(source, tmp_path):
    for i in range(6):
        source.add(f"/DCIM/100APPLE/IMG_000{i}.JPG")

    results = run_import(source, tmp_path)

    assert sum(isinstance(r, NewFile) for r in results) == 6
    assert source.max_concurrent_downloads >= 2


# --- layout ---


def test_layout_with_subdirectories_creates_them(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"x", mtime=MTIME)

    run_import(source, tmp_path, layout="{mtime:%Y}/{mtime:%m}/{name}")

    assert (tmp_path / "2024" / "01" / "IMG_0001.JPG").read_bytes() == b"x"


def test_layout_is_stored_and_reused_without_flag(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", mtime=MTIME)

    run_import(source, tmp_path, layout="{mtime:%Y}/{name}")
    source.add("/DCIM/100APPLE/IMG_0002.JPG", mtime=MTIME)
    run_import(source, tmp_path)

    assert (tmp_path / "2024" / "IMG_0002.JPG").exists()


def test_conflicting_layout_is_rejected(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")

    run_import(source, tmp_path, layout="{mtime:%Y}/{name}")

    with pytest.raises(LayoutConflictError):
        run_import(source, tmp_path, layout="{name}")


def test_force_layout_overrides_and_updates_stored(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", mtime=MTIME)

    run_import(source, tmp_path, layout="{mtime:%Y}/{name}")
    source.add("/DCIM/100APPLE/IMG_0002.JPG", mtime=MTIME)
    run_import(source, tmp_path, layout="flat_{name}", force_layout=True)
    source.add("/DCIM/100APPLE/IMG_0003.JPG", mtime=MTIME)
    run_import(source, tmp_path)

    assert (tmp_path / "flat_IMG_0002.JPG").exists()
    assert (tmp_path / "flat_IMG_0003.JPG").exists()


def test_default_layout_is_stored_on_first_run(source, tmp_path, open_db):
    source.add("/DCIM/100APPLE/IMG_0001.JPG")

    run_import(source, tmp_path)

    db = open_db(tmp_path / "media.db")
    assert db.get_setting("layout") == DEFAULT_LAYOUT


# --- download integrity ---


def test_incomplete_download_is_retried_then_failed(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata")  # 8 bytes
    for _ in range(3):  # 1 initial + 2 retries, all short
        source.truncate_next_download("/DCIM/100APPLE/IMG_0001.JPG", 4)

    results = run_import(source, tmp_path)

    failed = [r for r in results if isinstance(r, FailedFile)]
    assert [str(f.afc_path) for f in failed] == ["/DCIM/100APPLE/IMG_0001.JPG"]
    assert "incomplete" in failed[0].error
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".part"]


def test_transient_incomplete_download_is_retried_to_success(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    source.truncate_next_download("/DCIM/100APPLE/IMG_0001.JPG", 4)  # first try short

    results = run_import(source, tmp_path)

    assert any(isinstance(r, NewFile) for r in results)
    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG").read_bytes() == b"jpegdata"


def test_reconciles_file_after_complete_import_failure(tmp_path):
    source = FakeSource()
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    db = MediaDatabase(tmp_path / "media.db")
    layout = resolve_layout(db, None, False)

    def fail_complete_import(afc_path, st_size, st_mtime):
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    db.complete_import = fail_complete_import

    async def _collect():
        plan = await plan_import(source, db)
        first = [
            result
            async for result in execute_import(source, db, plan, tmp_path, layout)
        ]
        recovered = await plan_import(source, db)
        return first, recovered

    try:
        first, recovered = asyncio.run(_collect())
    finally:
        db.close()

    assert isinstance(first[0], FailedFile)
    assert not recovered.to_download
    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001.JPG").exists()
    assert not (tmp_path / "2024-01-02_03-04-05_IMG_0001_1.JPG").exists()


class _RecordFailsFor(InMemoryDb):
    """An in-memory database whose finalization fails for one device path."""

    def __init__(self, fail_path: str):
        super().__init__()
        self._fail_path = fail_path

    @override
    def complete_import(
        self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime
    ):
        if str(afc_path) == self._fail_path:
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)

        super().complete_import(afc_path, st_size, st_mtime)


def test_finalize_error_becomes_failed_file_and_continues(tmp_path):
    source = FakeSource()
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"aaaa", mtime=MTIME)
    source.add("/DCIM/100APPLE/IMG_0002.JPG", data=b"bbbb", mtime=MTIME)
    db = _RecordFailsFor("/DCIM/100APPLE/IMG_0001.JPG")

    async def _collect():
        plan = await plan_import(source, db)
        layout = resolve_layout(db, None, False)
        return [r async for r in execute_import(source, db, plan, tmp_path, layout)]

    results = asyncio.run(_collect())

    failed = [r for r in results if isinstance(r, FailedFile)]
    new = [r for r in results if isinstance(r, NewFile)]
    assert [str(f.afc_path) for f in failed] == ["/DCIM/100APPLE/IMG_0001.JPG"]
    assert [n.afc_path.name for n in new] == ["IMG_0002.JPG"]
