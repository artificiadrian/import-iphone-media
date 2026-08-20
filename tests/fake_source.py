"""In-memory MediaSource and Database fakes for importer tests."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from dcimport.importer import DirEntry, FileStat

DEFAULT_MTIME = datetime(2024, 1, 2, 3, 4, 5)


@dataclass
class FakeFile:
    data: bytes
    mtime: datetime


@dataclass
class FakeSource:
    """Simulates an iPhone DCIM tree. Failures are consumed one per download attempt;
    a failure writes half the file before raising, to simulate an interrupted transfer."""

    files: dict[str, FakeFile] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    download_failures: dict[str, list[BaseException]] = field(default_factory=dict)
    truncations: dict[str, list[int]] = field(default_factory=dict)
    stat_calls: Counter = field(default_factory=Counter)
    download_calls: Counter = field(default_factory=Counter)
    device_name: str | None = None
    active_downloads: int = 0
    max_concurrent_downloads: int = 0
    active_stats: int = 0
    max_concurrent_stats: int = 0

    def add(self, path: str, data: bytes = b"x", mtime: datetime = DEFAULT_MTIME):
        self.files[path] = FakeFile(data=data, mtime=mtime)
        parent = str(PurePosixPath(path).parent)
        while parent not in ("/", ""):
            self.dirs.add(parent)
            parent = str(PurePosixPath(parent).parent)

    def fail_next_download(self, path: str, error: BaseException):
        self.download_failures.setdefault(path, []).append(error)

    def truncate_next_download(self, path: str, nbytes: int):
        """Make the next download of `path` write only `nbytes` and return without error,
        simulating a silent short read (an early EOF that isn't reported as a failure)."""

        self.truncations.setdefault(path, []).append(nbytes)

    async def list_files(self, path: PurePosixPath) -> AsyncIterator[PurePosixPath]:
        prefix = str(path).rstrip("/") + "/"
        for d in sorted(self.dirs):
            if d.startswith(prefix):
                yield PurePosixPath(d)
        for f in sorted(self.files):
            if f.startswith(prefix):
                yield PurePosixPath(f)

    async def stat(self, path: PurePosixPath):
        self.stat_calls[str(path)] += 1
        self.active_stats += 1
        self.max_concurrent_stats = max(self.max_concurrent_stats, self.active_stats)

        try:
            # yield to the event loop so overlapping stats are observable
            await asyncio.sleep(0)

            if str(path) in self.dirs:
                return DirEntry()

            file = self.files[str(path)]
            return FileStat(size=len(file.data), mtime=file.mtime)
        finally:
            self.active_stats -= 1

    async def download(self, path: PurePosixPath, target: BinaryIO):
        self.download_calls[str(path)] += 1
        self.active_downloads += 1
        self.max_concurrent_downloads = max(
            self.max_concurrent_downloads, self.active_downloads
        )

        try:
            # yield to the event loop so overlapping downloads are observable
            await asyncio.sleep(0)

            file = self.files[str(path)]
            failures = self.download_failures.get(str(path))

            if failures:
                target.write(file.data[: len(file.data) // 2])
                raise failures.pop(0)

            truncations = self.truncations.get(str(path))

            if truncations:
                target.write(file.data[: truncations.pop(0)])
                return

            target.write(file.data)
        finally:
            self.active_downloads -= 1

    async def close(self):
        pass


@dataclass
class InMemoryDb:
    """In-memory Database implementation: a dedup set plus a settings dict."""

    imported: set[tuple[str, int, datetime]] = field(default_factory=set)
    settings: dict[str, str] = field(default_factory=dict)
    pending: dict[tuple[str, int, datetime], tuple[Path, int]] = field(
        default_factory=dict
    )

    def contains(self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime):
        return (str(afc_path), st_size, st_mtime) in self.imported

    def begin_import(
        self,
        afc_path: PurePosixPath,
        st_size: int,
        st_mtime: datetime,
        local_path: Path,
        local_size: int,
    ):
        self.pending[(str(afc_path), st_size, st_mtime)] = (local_path, local_size)

    def complete_import(
        self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime
    ):
        key = (str(afc_path), st_size, st_mtime)
        self.imported.add(key)
        self.pending.pop(key, None)

    def reconcile_pending_imports(self):
        for key, (target, local_size) in self.pending.copy().items():
            if (
                target.is_file()
                and not target.is_symlink()
                and target.stat().st_size == local_size
            ):
                self.imported.add(key)

            self.pending.pop(key)

    def get_setting(self, key: str):
        return self.settings.get(key)

    def set_setting(self, key: str, value: str):
        self.settings[key] = value

    def close(self):
        pass
