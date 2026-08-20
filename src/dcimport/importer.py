import asyncio
import itertools
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from dcimport import heic
from dcimport.immutable import immutable
from dcimport.layout import DEFAULT_LAYOUT, Layout, parse_layout

DCIM_PATH = "/DCIM"
INCLUDE_EXTENSIONS = ("jpg", "jpeg", "png", "mov", "mp4", "heic")
DEFAULT_DB_NAME = "media.db"

LAYOUT_SETTING = "layout"

LIVE_PHOTO_IMAGE_EXTENSIONS = (".heic", ".jpg", ".jpeg")
LIVE_PHOTO_VIDEO_EXTENSION = ".mov"


def _require_at_least(value: int, minimum: int, name: str) -> None:
    if value < minimum:
        msg = f"{name} must be at least {minimum}"
        raise ValueError(msg)


class LayoutConflictError(Exception):
    """A layout was requested that differs from the one stored in the library's database."""

    def __init__(self, stored: str, requested: str):
        super().__init__(
            f"The requested layout '{requested}' does not match this library's stored layout '{stored}'."
        )
        self.stored = stored
        self.requested = requested


@immutable
class FileStat:
    """Parsed metadata of a file on the device."""

    size: int
    mtime: datetime


@immutable
class DirEntry:
    """A directory entry on the device; never downloaded."""


class MediaSource(Protocol):
    """A device filesystem that media files can be listed, stat'ed and downloaded from."""

    def list_files(self, path: PurePosixPath) -> AsyncIterator[PurePosixPath]:
        """Recursively yield all entries (files and directories) under `path`."""
        ...

    async def stat(self, path: PurePosixPath) -> FileStat | DirEntry:
        """Return the metadata of the entry at `path`."""
        ...

    async def download(self, path: PurePosixPath, target: BinaryIO) -> None:
        """Download the file at `path` to the local `target` path."""
        ...


class Database(Protocol):
    """The library database used for deduplication, import recovery, and settings."""

    def contains(
        self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime
    ) -> bool: ...

    def begin_import(
        self,
        afc_path: PurePosixPath,
        st_size: int,
        st_mtime: datetime,
        local_path: Path,
        local_size: int,
    ) -> None: ...

    def complete_import(
        self, afc_path: PurePosixPath, st_size: int, st_mtime: datetime
    ) -> None: ...

    def reconcile_pending_imports(self) -> None: ...

    def get_setting(self, key: str) -> str | None: ...

    def set_setting(self, key: str, value: str) -> None: ...


@immutable
class NewFile:
    """A new media file that has been downloaded from the device."""

    afc_path: PurePosixPath
    local_path: Path
    stat: FileStat


@immutable
class FailedFile:
    """A file whose download failed after all retries; the import continues with the next file."""

    afc_path: PurePosixPath
    stat: FileStat
    error: str


@immutable
class PlannedFile:
    """A media file found on the device during scanning."""

    afc_path: PurePosixPath
    stat: FileStat


@immutable
class ImportPlan:
    """Result of scanning the device: what to download, what is already imported,
    and what is ignored (directories and excluded extensions)."""

    to_download: tuple[PlannedFile, ...]
    existing: tuple[PlannedFile, ...]
    ignored: tuple[PurePosixPath, ...]

    @property
    def total_bytes(self):
        """Total size of all files that would be downloaded."""

        return sum(f.stat.size for f in self.to_download)


def resolve_layout(db: Database, requested: str | None, force: bool):
    """Resolve the layout for this run: the stored one by default, storing on first
    use; a differing request errors unless `force`, which updates the stored value.
    Device-independent — call it before connecting so bad layouts fail fast.

    Raises:
        InvalidLayoutError: If `requested` is malformed.
        LayoutConflictError: If `requested` differs from the stored one and not `force`."""

    stored = db.get_setting(LAYOUT_SETTING)

    if (
        stored is not None
        and requested is not None
        and requested != stored
        and not force
    ):
        raise LayoutConflictError(stored=stored, requested=requested)

    template = requested if requested is not None else (stored or DEFAULT_LAYOUT)
    layout = parse_layout(template)

    if template != stored:
        db.set_setting(LAYOUT_SETTING, template)

    return layout


def _is_live_photo_video(path: PurePosixPath, image_keys: set[tuple[str, str]]):
    """A Live Photo's video half: a .mov whose stem matches an image in the same directory."""

    return (
        path.suffix.lower() == LIVE_PHOTO_VIDEO_EXTENSION
        and (str(path.parent), path.stem.lower()) in image_keys
    )


def _available_path(target: Path, reserved: set[Path]):
    """Return `target` if free, otherwise the first `name_1`, `name_2`, … that is.
    `reserved` holds paths already claimed by this run but not yet on disk."""

    n = 0
    candidate = target

    while os.path.lexists(candidate) or candidate in reserved:
        n += 1
        candidate = target.with_name(f"{target.stem}_{n}{target.suffix}")

    return candidate


async def plan_import(
    source: MediaSource,
    db: Database,
    dcim_path: str = DCIM_PATH,
    include_extensions: Sequence[str] = INCLUDE_EXTENSIONS,
    skip_live_videos: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
    on_scan_progress: Callable[[int], None] | None = None,
    stat_concurrency: int = 16,
):
    """Reconcile completed pending imports, then scan and classify every device entry.
    With `skip_live_videos`, the video halves of Live Photos are ignored. `since`/`until`
    bound the files to import by modification time (inclusive). `on_scan_progress` is
    called with the running count of files stat'ed, for live feedback during the scan."""

    _require_at_least(stat_concurrency, 1, "stat_concurrency")

    db.reconcile_pending_imports()

    wanted_extensions = {
        normalized
        for ext in include_extensions
        if (normalized := ext.lower().lstrip("."))
    }
    entries = [entry async for entry in source.list_files(PurePosixPath(dcim_path))]

    image_keys = {
        (str(path.parent), path.stem.lower())
        for path in entries
        if path.suffix.lower() in LIVE_PHOTO_IMAGE_EXTENSIONS
    }

    ignored = []
    candidates = []

    for path in entries:
        # filter on extension (and Live Photo videos) before stat'ing, to save a
        # USB round-trip per file that would be ignored anyway
        if path.suffix.lower()[1:] not in wanted_extensions or (
            skip_live_videos and _is_live_photo_video(path, image_keys)
        ):
            ignored.append(path)
        else:
            candidates.append(path)

    stats = await _stat_all(source, candidates, stat_concurrency, on_scan_progress)

    to_download, existing = [], []

    for path, stat in zip(candidates, stats, strict=True):
        if isinstance(stat, DirEntry) or _outside_dates(stat, since, until):
            ignored.append(path)
            continue

        planned = PlannedFile(afc_path=path, stat=stat)

        if db.contains(path, stat.size, stat.mtime):
            existing.append(planned)
        else:
            to_download.append(planned)

    return ImportPlan(
        to_download=tuple(to_download),
        existing=tuple(existing),
        ignored=tuple(ignored),
    )


def _outside_dates(stat: FileStat, since: datetime | None, until: datetime | None):
    """Whether a file's modification time falls outside the inclusive [since, until] range."""

    return (since is not None and stat.mtime < since) or (
        until is not None and stat.mtime > until
    )


async def _stat_all(
    source: MediaSource,
    paths: Sequence[PurePosixPath],
    concurrency: int,
    on_progress: Callable[[int], None] | None,
):
    """Stat every path concurrently (one USB round-trip each), preserving input order.
    Reports a running count to `on_progress` as each stat completes."""

    completed = itertools.count(1)
    stats: list[FileStat | DirEntry | None] = [None] * len(paths)

    async def stat_one(index: int, path: PurePosixPath):
        stat = await source.stat(path)
        if on_progress is not None:
            on_progress(next(completed))
        return index, stat

    indexed_paths = iter(enumerate(paths))

    async with asyncio.TaskGroup() as task_group:
        pending = {
            task_group.create_task(stat_one(index, path))
            for index, path in itertools.islice(indexed_paths, concurrency)
        }

        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )

            for task in done:
                index, result = task.result()
                stats[index] = result

                if next_item := next(indexed_paths, None):
                    next_index, next_path = next_item
                    pending.add(task_group.create_task(stat_one(next_index, next_path)))

    return [stat for stat in stats if stat is not None]


async def _download_to_temp(
    source: MediaSource,
    path: PurePosixPath,
    target_path: Path,
    expected_size: int,
    retries: int,
):
    """Download `path` into `temp_path`, retrying on error or a short read. Returns
    None on success, or the last error after exhausting retries. Cleans up the temp
    file and re-raises on cancellation/KeyboardInterrupt (never retried)."""

    download_error = None

    for _attempt in range(retries + 1):
        descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=f".{target_path.name}.",
            suffix=".part",
            dir=target_path.parent,
        )
        temp_path = Path(raw_temp_path)

        try:
            with os.fdopen(descriptor, "w+b") as local_file:
                await source.download(path, local_file)
                local_file.flush()
                actual_size = os.fstat(local_file.fileno()).st_size
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            download_error = e
            continue
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        if actual_size != expected_size:
            # a silent short read (early EOF) would otherwise be recorded as a
            # successful import; treat it as a failure so it retries / re-imports
            temp_path.unlink(missing_ok=True)
            download_error = OSError(
                f"incomplete download: got {actual_size} of {expected_size} bytes"
            )
            continue

        return temp_path

    return download_error or OSError("download failed")


async def _download_file(
    source: MediaSource,
    db: Database,
    planned: PlannedFile,
    target_path: Path,
    converting: bool,
    download_retries: int,
):
    """Download one planned file into place; returns NewFile or FailedFile."""

    path, stat = planned.afc_path, planned.stat
    target_path.parent.mkdir(parents=True, exist_ok=True)

    download_result = await _download_to_temp(
        source, path, target_path, stat.size, download_retries
    )

    if isinstance(download_result, Exception):
        return FailedFile(afc_path=path, stat=stat, error=str(download_result))

    temp_path = download_result

    if converting:
        descriptor, raw_converted_path = tempfile.mkstemp(
            prefix=f".{target_path.name}.",
            suffix=".converted.part",
            dir=target_path.parent,
        )
        converted_path = Path(raw_converted_path)

        try:
            with os.fdopen(descriptor, "w+b") as converted_file:
                await asyncio.to_thread(
                    heic.convert_heic_to_jpeg,
                    temp_path,
                    converted_file,
                )
                converted_file.flush()
        except Exception as e:
            converted_path.unlink(missing_ok=True)
            return FailedFile(
                afc_path=path, stat=stat, error=f"HEIC conversion failed: {e}"
            )
        except BaseException:
            # cancellation/KeyboardInterrupt: don't leave a half-converted file
            converted_path.unlink(missing_ok=True)
            raise
        finally:
            temp_path.unlink(missing_ok=True)

        temp_path = converted_path

    if os.path.lexists(target_path):
        temp_path.unlink(missing_ok=True)
        error = f"failed to finalize: target was created while importing: {target_path}"
        return FailedFile(afc_path=path, stat=stat, error=error)

    try:
        os.utime(temp_path, (datetime.now().timestamp(), stat.mtime.timestamp()))
        local_size = temp_path.stat().st_size
        db.begin_import(
            path,
            stat.size,
            stat.mtime,
            target_path,
            local_size,
        )
        os.replace(temp_path, target_path)
        db.complete_import(path, stat.size, stat.mtime)
    except Exception as e:
        # a failure finalizing one file (e.g. a momentarily locked db) must not
        # escape and cancel the whole TaskGroup — degrade it to a FailedFile
        temp_path.unlink(missing_ok=True)
        return FailedFile(afc_path=path, stat=stat, error=f"failed to finalize: {e}")
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return NewFile(afc_path=path, local_path=target_path, stat=stat)


async def execute_import(
    source: MediaSource,
    db: Database,
    plan: ImportPlan,
    output_path: Path,
    layout: Layout,
    download_retries: int = 2,
    convert_heic: bool = False,
    concurrency: int = 4,
):
    """Download every file in `plan.to_download` into `output_path` under the resolved
    `layout`, recording each in `db` once it is fully in place. Up to `concurrency`
    files transfer at once; yields one result per attempted file, in completion order.

    Raises:
        MissingHeicSupportError: If `convert_heic` is set but pillow-heif is not installed."""

    _require_at_least(concurrency, 1, "concurrency")
    _require_at_least(download_retries, 0, "download_retries")

    if convert_heic and not heic.heic_support_available():
        raise heic.MissingHeicSupportError()

    output_path.mkdir(parents=True, exist_ok=True)

    reserved: set[Path] = set()
    jobs = []

    for planned in plan.to_download:
        converting = convert_heic and planned.afc_path.suffix.lower() == ".heic"
        target_name = (
            f"{planned.afc_path.stem}.jpg" if converting else planned.afc_path.name
        )
        rendered_path = layout.render(name=target_name, mtime=planned.stat.mtime)
        target_path = _available_path(
            output_path / rendered_path,
            reserved,
        )
        reserved.add(target_path)
        jobs.append((planned, target_path, converting))

    pending_jobs = iter(jobs)

    async def download_one(
        planned: PlannedFile, target_path: Path, converting: bool
    ) -> NewFile | FailedFile | BaseException:
        try:
            return await _download_file(
                source,
                db,
                planned,
                target_path,
                converting,
                download_retries,
            )
        except (KeyboardInterrupt, SystemExit) as e:
            return e

    def create_task(
        task_group: asyncio.TaskGroup,
        job: tuple[PlannedFile, Path, bool],
    ):
        return task_group.create_task(download_one(*job))

    async with asyncio.TaskGroup() as task_group:
        pending = {
            create_task(task_group, job)
            for job in itertools.islice(pending_jobs, concurrency)
        }

        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )

            for task in done:
                result = task.result()

                if isinstance(result, BaseException):
                    raise result

                if next_job := next(pending_jobs, None):
                    pending.add(create_task(task_group, next_job))

                yield result
