import asyncio
from datetime import datetime
from pathlib import PurePosixPath

import pytest
from typing_extensions import override

from dcimport.importer import FileStat, plan_import
from tests.fake_source import DEFAULT_MTIME as MTIME
from tests.fake_source import FakeSource, InMemoryDb


def make_source():
    source = FakeSource()
    source.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"12345", mtime=MTIME)
    source.add("/DCIM/100APPLE/IMG_0002.MOV", data=b"1234567890", mtime=MTIME)
    source.add("/DCIM/100APPLE/IMG_0003.AAE", data=b"xml", mtime=MTIME)
    return source


def test_plan_lists_new_files_with_total_bytes():
    plan = asyncio.run(plan_import(make_source(), InMemoryDb()))

    assert sorted(p.afc_path.name for p in plan.to_download) == [
        "IMG_0001.JPG",
        "IMG_0002.MOV",
    ]
    assert plan.total_bytes == 15


def test_plan_separates_already_imported_files(tmp_path):
    db = InMemoryDb()
    path = PurePosixPath("/DCIM/100APPLE/IMG_0001.JPG")
    db.begin_import(path, 5, MTIME, tmp_path / "IMG_0001.JPG", 5)
    db.complete_import(path, 5, MTIME)

    plan = asyncio.run(plan_import(make_source(), db))

    assert [p.afc_path.name for p in plan.existing] == ["IMG_0001.JPG"]
    assert [p.afc_path.name for p in plan.to_download] == ["IMG_0002.MOV"]
    assert plan.total_bytes == 10


def test_plan_counts_ignored_entries():
    plan = asyncio.run(plan_import(make_source(), InMemoryDb()))

    ignored_names = [p.name for p in plan.ignored]
    assert "IMG_0003.AAE" in ignored_names
    assert "100APPLE" in ignored_names


def test_extensions_match_regardless_of_case_and_leading_dot():
    plan = asyncio.run(
        plan_import(make_source(), InMemoryDb(), include_extensions=("JPG", ".mov"))
    )

    assert sorted(p.afc_path.name for p in plan.to_download) == [
        "IMG_0001.JPG",
        "IMG_0002.MOV",
    ]


def test_empty_extension_token_does_not_stat_directories():
    source = make_source()

    asyncio.run(plan_import(source, InMemoryDb(), include_extensions=("jpg", "")))

    # an empty token must not match extensionless entries (e.g. directories),
    # which would otherwise cost a wasted stat round-trip before being ignored
    assert source.stat_calls["/DCIM/100APPLE"] == 0


def test_plan_downloads_nothing_and_records_nothing(tmp_path):
    source = make_source()
    db = InMemoryDb()

    asyncio.run(plan_import(source, db))

    assert source.download_calls == {}
    assert db.imported == set()
    assert list(tmp_path.iterdir()) == []


def _dated_source():
    source = FakeSource()
    source.add("/DCIM/100APPLE/OLD.JPG", mtime=datetime(2023, 1, 1))
    source.add("/DCIM/100APPLE/NEW.JPG", mtime=datetime(2024, 6, 1))
    return source


def test_since_filters_out_older_files():
    plan = asyncio.run(
        plan_import(_dated_source(), InMemoryDb(), since=datetime(2024, 1, 1))
    )

    assert [p.afc_path.name for p in plan.to_download] == ["NEW.JPG"]
    assert PurePosixPath("/DCIM/100APPLE/OLD.JPG") in plan.ignored


def test_until_filters_out_newer_files():
    plan = asyncio.run(
        plan_import(_dated_source(), InMemoryDb(), until=datetime(2024, 1, 1))
    )

    assert [p.afc_path.name for p in plan.to_download] == ["OLD.JPG"]
    assert PurePosixPath("/DCIM/100APPLE/NEW.JPG") in plan.ignored


def test_scan_stats_files_concurrently():
    source = FakeSource()
    for i in range(6):
        source.add(f"/DCIM/100APPLE/IMG_000{i}.JPG")

    asyncio.run(plan_import(source, InMemoryDb()))

    assert source.max_concurrent_stats >= 2


def test_scan_reports_running_count():
    counts = []

    asyncio.run(
        plan_import(make_source(), InMemoryDb(), on_scan_progress=counts.append)
    )

    # one callback per stat'd candidate (the two wanted files), counting up
    assert counts == [1, 2]


class BlockingStatSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    @override
    async def stat(self, path):
        self.stat_calls[str(path)] += 1
        self.active_stats += 1
        self.max_concurrent_stats = max(self.max_concurrent_stats, self.active_stats)

        if self.active_stats == 2:
            self.started.set()

        try:
            await self.gate.wait()
            file = self.files[str(path)]
            return FileStat(size=len(file.data), mtime=file.mtime)
        finally:
            self.active_stats -= 1


class RollingStatSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.first_gate = asyncio.Event()
        self.third_started = asyncio.Event()

    @override
    async def stat(self, path):
        if path.name == "IMG_0000.JPG":
            await self.first_gate.wait()
        elif path.name == "IMG_0002.JPG":
            self.third_started.set()

        file = self.files[str(path)]
        return FileStat(size=len(file.data), mtime=file.mtime)


def test_plan_rejects_nonpositive_stat_concurrency():
    source = make_source()

    async def plan():
        return await plan_import(source, InMemoryDb(), stat_concurrency=0)

    with pytest.raises(ValueError, match="stat_concurrency"):
        asyncio.run(asyncio.wait_for(plan(), timeout=0.01))


def test_plan_creates_only_one_batch_of_stat_tasks():
    source = BlockingStatSource()
    for i in range(100):
        source.add(f"/DCIM/100APPLE/IMG_{i:04}.JPG")

    async def plan():
        task = asyncio.create_task(
            plan_import(source, InMemoryDb(), stat_concurrency=2)
        )
        await source.started.wait()
        pending = len(asyncio.all_tasks()) - 1
        source.gate.set()
        await task
        return pending

    assert asyncio.run(plan()) <= 3


def test_plan_reuses_available_stat_slot():
    source = RollingStatSource()
    for i in range(3):
        source.add(f"/DCIM/100APPLE/IMG_{i:04}.JPG")

    async def plan():
        task = asyncio.create_task(
            plan_import(source, InMemoryDb(), stat_concurrency=2)
        )
        await asyncio.wait_for(source.third_started.wait(), timeout=1)
        source.first_gate.set()
        return await task

    result = asyncio.run(plan())

    assert len(result.to_download) == 3
