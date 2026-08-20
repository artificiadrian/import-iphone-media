import argparse
import asyncio
import json
import math
import sys
from collections.abc import Sequence
from datetime import datetime, time
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rich.console import Console
from rich.filesize import decimal as human_size
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

from dcimport.afc_utils import (
    DEFAULT_OPERATION_TIMEOUT,
    DEFAULT_PAIR_TIMEOUT,
    AfcSource,
    MultipleDevicesError,
    afc_connect,
)
from dcimport.db import LegacyTimezoneMigrationError, MediaDatabase
from dcimport.heic import MissingHeicSupportError, heic_support_available
from dcimport.immutable import immutable
from dcimport.importer import (
    DCIM_PATH,
    DEFAULT_DB_NAME,
    INCLUDE_EXTENSIONS,
    FailedFile,
    ImportPlan,
    LayoutConflictError,
    NewFile,
    execute_import,
    plan_import,
    resolve_layout,
)
from dcimport.layout import InvalidLayoutError, Layout, parse_layout


def _parse_date(value: str):
    """Parse a YYYY-MM-DD date for the --since/--until options."""

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as e:
        msg = f"invalid date '{value}', expected YYYY-MM-DD"
        raise argparse.ArgumentTypeError(msg) from e


def _parse_timezone(value: str) -> ZoneInfo:
    """Parse an IANA timezone for a legacy media database migration."""

    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as e:
        msg = f"unknown IANA timezone '{value}'"
        raise argparse.ArgumentTypeError(msg) from e


def _parse_legacy_fold(value: str) -> int:
    folds = {"earlier": 0, "later": 1}
    try:
        return folds[value]
    except KeyError as e:
        msg = "legacy fold must be 'earlier' or 'later'"
        raise argparse.ArgumentTypeError(msg) from e


def _parse_positive_float(value: str) -> float:
    """Parse a finite positive decimal command-line value."""

    try:
        parsed = float(value)
    except ValueError as e:
        msg = f"invalid number '{value}'"
        raise argparse.ArgumentTypeError(msg) from e

    if not math.isfinite(parsed) or parsed <= 0:
        msg = f"'{value}' must be greater than zero"
        raise argparse.ArgumentTypeError(msg)

    return parsed


def _parse_nonnegative_int(value: str) -> int:
    """Parse a nonnegative integer command-line value."""

    try:
        parsed = int(value)
    except ValueError as e:
        msg = f"invalid integer '{value}'"
        raise argparse.ArgumentTypeError(msg) from e

    if parsed < 0:
        msg = f"'{value}' must not be negative"
        raise argparse.ArgumentTypeError(msg)

    return parsed


def _parse_positive_int(value: str) -> int:
    """Parse an integer command-line value greater than zero."""

    parsed = _parse_nonnegative_int(value)
    if parsed == 0:
        msg = f"'{value}' must be greater than zero"
        raise argparse.ArgumentTypeError(msg)

    return parsed


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Import media files from iPhone",
        add_help=False,
    )
    device_options = parser.add_argument_group("device options")
    selection_options = parser.add_argument_group("file selection")
    output_options = parser.add_argument_group("output options")
    performance_options = parser.add_argument_group("performance and retries")
    migration_options = parser.add_argument_group("legacy database migration")
    diagnostics_options = parser.add_argument_group("diagnostics")

    parser.add_argument(
        "output",
        metavar="DIRECTORY",
        help="Directory where media files should be downloaded to",
        type=Path,
    )

    device_options.add_argument(
        "--dcim-path",
        metavar="PATH",
        help="Directory on iPhone to scan for media files",
        default=DCIM_PATH,
    )

    output_options.add_argument(
        "--db-path",
        metavar="PATH",
        help="Library database location (default: media.db in the output directory)",
        type=Path,
    )

    migration_options.add_argument(
        "--legacy-timezone",
        metavar="ZONE",
        help="IANA timezone used by records in a pre-0.2 media.db",
        type=_parse_timezone,
    )

    migration_options.add_argument(
        "--legacy-fold",
        metavar="{earlier,later}",
        help="Resolve ambiguous daylight-saving timestamps",
        type=_parse_legacy_fold,
    )

    selection_options.add_argument(
        "--include-extensions",
        metavar="EXT,EXT,...",
        help="List of file extensions to include (comma-separated)",
        default=",".join(INCLUDE_EXTENSIONS),
    )

    output_options.add_argument(
        "--layout",
        metavar="TEMPLATE",
        help="Filename/subfolder template of {name} and {mtime:...}; stored and reused (default: timestamped)",
    )

    output_options.add_argument(
        "--force",
        help="Allow changing the layout stored in the database",
        action="store_true",
    )

    device_options.add_argument(
        "--udid",
        help="Device to import from, by UDID (needed when multiple are connected)",
    )

    selection_options.add_argument(
        "--skip-live-videos",
        help="Skip the video half of Live Photos",
        action="store_true",
    )

    output_options.add_argument(
        "--convert-heic",
        help="Convert HEIC photos to JPEG (needs the 'heic' extra)",
        action="store_true",
    )

    selection_options.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only import files modified on or after this date",
        type=_parse_date,
    )

    selection_options.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="Only import files modified on or before this date",
        type=_parse_date,
    )

    output_options.add_argument(
        "--manifest",
        metavar="PATH",
        help="Write a JSON report of imported and failed files",
        type=Path,
    )

    performance_options.add_argument(
        "--concurrency",
        metavar="N",
        help="Number of files to download in parallel",
        type=_parse_positive_int,
        default=4,
    )

    performance_options.add_argument(
        "--retries",
        metavar="N",
        help="Retries per file before giving up",
        type=_parse_nonnegative_int,
        default=2,
    )

    device_options.add_argument(
        "--operation-timeout",
        metavar="SECONDS",
        help="Maximum seconds to wait for each AFC operation",
        type=_parse_positive_float,
        default=DEFAULT_OPERATION_TIMEOUT,
    )

    device_options.add_argument(
        "--pair-timeout",
        metavar="SECONDS",
        help="Maximum seconds to wait for the initial trust prompt",
        type=_parse_positive_float,
        default=DEFAULT_PAIR_TIMEOUT,
    )

    diagnostics_options.add_argument(
        "-h",
        "--help",
        action="help",
        help="Show this help message and exit",
    )

    diagnostics_options.add_argument(
        "--verbose",
        help="Enable verbose output",
        action="store_true",
    )

    diagnostics_options.add_argument(
        "--version",
        action="version",
        version=version("dcimport"),
    )

    args = parser.parse_args()

    if args.legacy_fold is not None and args.legacy_timezone is None:
        parser.error("--legacy-fold requires --legacy-timezone")

    return args


def cli():
    args = _parse_args()

    include_extensions = tuple(
        ext.strip() for ext in args.include_extensions.split(",")
    )

    # a bare date bounds the whole day: --since starts at 00:00, --until ends at 23:59:59
    since = datetime.combine(args.since, time.min) if args.since else None
    until = datetime.combine(args.until, time.max) if args.until else None

    sys.exit(
        main(
            output_path=args.output,
            dcim_path=args.dcim_path,
            db_path=args.db_path,
            include_extensions=include_extensions,
            layout=args.layout,
            force_layout=args.force,
            udid=args.udid,
            convert_heic=args.convert_heic,
            skip_live_videos=args.skip_live_videos,
            verbose=args.verbose,
            concurrency=args.concurrency,
            download_retries=args.retries,
            operation_timeout=args.operation_timeout,
            pair_timeout=args.pair_timeout,
            since=since,
            until=until,
            manifest=args.manifest,
            legacy_timezone=args.legacy_timezone,
            legacy_fold=args.legacy_fold,
        )
    )


@immutable
class ImportConfig:
    """All settings for one import run, parsed once from the CLI."""

    output_path: Path
    dcim_path: str
    db_path: Path | None
    include_extensions: Sequence[str]
    layout: str | None
    force_layout: bool
    udid: str | None
    convert_heic: bool
    skip_live_videos: bool
    verbose: bool
    concurrency: int
    download_retries: int
    operation_timeout: float
    pair_timeout: float
    since: datetime | None
    until: datetime | None
    manifest: Path | None
    legacy_timezone: ZoneInfo | None
    legacy_fold: int | None


class _RunStats:
    """Running tally of a download phase; feeds the progress description and summary line.
    `existing` is fixed at scan time; `new`/`failed` accumulate as downloads complete."""

    def __init__(self, existing: int):
        self._existing = existing
        self._new = 0
        self._failed = 0

    @property
    def completed(self):
        return self._new + self._failed

    @property
    def failed(self):
        return self._failed

    def record(self, result: NewFile | FailedFile):
        if isinstance(result, NewFile):
            self._new += 1
        else:
            self._failed += 1

    def has_failures(self):
        return self._failed > 0

    def line(self):
        text = Text("Imported ")
        text.append_text(_file_phrase(self._new, "new", "bold green"))
        text.append("; skipped ")
        text.append_text(_file_phrase(self._existing, "existing", "yellow"))

        if self._failed:
            text.append("; ")
            text.append_text(_file_phrase(self._failed, "failed", "bold red"))

        text.append(".")
        return text


def _file_phrase(count: int, label: str, style: str) -> Text:
    noun = "file" if count == 1 else "files"
    return Text(f"{count} {label} {noun}", style=style)


def _plan_summary(plan: ImportPlan) -> Text:
    text = Text("Found ")
    text.append_text(_file_phrase(len(plan.to_download), "new", "bold green"))
    text.append(f" ({human_size(plan.total_bytes)})", style="dim")
    text.append(", ")
    text.append_text(_file_phrase(len(plan.existing), "existing", "yellow"))
    text.append(", ")
    text.append_text(_file_phrase(len(plan.ignored), "ignored", "dim"))
    text.append(".")
    return text


async def _download_all(
    console: Console,
    config: ImportConfig,
    stats: _RunStats,
    source: AfcSource,
    db: MediaDatabase,
    plan: ImportPlan,
    layout: Layout,
):
    """Download everything in `plan` with a progress bar, updating `stats` as it goes.
    Returns every download result (new and failed), in completion order."""

    results: list[NewFile | FailedFile] = []

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Importing", total=plan.total_bytes)

        async for file in execute_import(
            source,
            db,
            plan,
            config.output_path,
            layout,
            download_retries=config.download_retries,
            convert_heic=config.convert_heic,
            concurrency=config.concurrency,
        ):
            stats.record(file)
            results.append(file)

            if config.verbose:
                detail = Text(
                    "Imported " if isinstance(file, NewFile) else "Failed ",
                    style="green" if isinstance(file, NewFile) else "red",
                )
                detail.append(str(file.afc_path), style="cyan")

                if isinstance(file, NewFile):
                    detail.append(" → ")
                    detail.append(str(file.local_path), style="cyan")
                else:
                    detail.append(f": {file.error}")

                progress.console.print(detail)

            progress.advance(task, file.stat.size)
            total_files = len(plan.to_download)
            noun = "file" if total_files == 1 else "files"
            description = f"Importing {stats.completed}/{total_files} {noun}"
            if stats.failed:
                description += f" ([bold red]{stats.failed} failed[/])"
            progress.update(task, description=description)

    return results


def _report_failures(console: Console, results: list[NewFile | FailedFile]):
    """List the files that could not be downloaded, so they aren't lost in the scrollback."""

    failed = [r for r in results if isinstance(r, FailedFile)]

    if not failed:
        return

    console.print(Text("\nFailed files:", style="bold red"))

    for file in failed:
        detail = Text("  ")
        detail.append(str(file.afc_path), style="cyan")
        detail.append(f"\n    {file.error}")
        console.print(detail)

    console.print("\nRun the same command to retry failed files.")


def _write_manifest(path: Path, plan: ImportPlan, results: list[NewFile | FailedFile]):
    """Write a JSON report of the run: imported files, failures, and skip/ignore counts."""

    manifest = {
        "imported": [
            {
                "afc_path": str(r.afc_path),
                "local_path": str(r.local_path),
                "size": r.stat.size,
                "mtime": r.stat.mtime.isoformat(),
            }
            for r in results
            if isinstance(r, NewFile)
        ],
        "failed": [
            {"afc_path": str(r.afc_path), "size": r.stat.size, "error": r.error}
            for r in results
            if isinstance(r, FailedFile)
        ],
        "skipped_existing": len(plan.existing),
        "ignored": len(plan.ignored),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))


async def _run_import(console: Console, config: ImportConfig):
    """Connect, scan, and download; returns the exit code. Failures the user
    needs to act on propagate as exceptions to `main`."""

    # fail fast on device-independent problems, before any side effects or the phone.
    # validate the layout's syntax up front so a typo doesn't create an output dir/db
    # (the conflict check needs the db and happens in resolve_layout below)
    if config.convert_heic and not heic_support_available():
        raise MissingHeicSupportError()

    if config.layout is not None:
        parse_layout(config.layout)

    config.output_path.mkdir(parents=True, exist_ok=True)
    db = MediaDatabase(
        config.db_path or config.output_path / DEFAULT_DB_NAME,
        legacy_timezone=config.legacy_timezone,
        legacy_fold=config.legacy_fold,
    )

    try:
        layout = resolve_layout(db, config.layout, config.force_layout)

        source = None

        try:
            with console.status("Connecting to your device…"):
                source = await afc_connect(
                    udid=config.udid,
                    operation_timeout=config.operation_timeout,
                    pair_timeout=config.pair_timeout,
                )

            device_name = source.device_name or "your device"
            introduction = Text("Importing from ")
            introduction.append(device_name, style="bold")
            introduction.append(": ")
            introduction.append(config.dcim_path, style="cyan")
            introduction.append(" → ")
            introduction.append(str(config.output_path.absolute()), style="cyan")
            console.print(introduction)

            with console.status("Scanning for media files…") as status:
                plan = await plan_import(
                    source,
                    db,
                    config.dcim_path,
                    config.include_extensions,
                    skip_live_videos=config.skip_live_videos,
                    since=config.since,
                    until=config.until,
                    on_scan_progress=lambda n: status.update(
                        f"Scanning for media files… ({n} found)"
                    ),
                )

            console.print(_plan_summary(plan))

            if not plan.to_download:
                up_to_date = Text("Up to date.", style="bold green")
                up_to_date.append(" No new files to import.")
                console.print(up_to_date)
                if config.manifest is not None:
                    _write_manifest(config.manifest, plan, [])
                return 0

            stats = _RunStats(existing=len(plan.existing))
            results = await _download_all(
                console, config, stats, source, db, plan, layout
            )

            _report_failures(console, results)

            if config.manifest is not None:
                _write_manifest(config.manifest, plan, results)

            if stats.has_failures():
                summary = Text("Import complete with failures.", style="bold yellow")
                summary.append(" ")
                summary.append_text(stats.line())
                console.print(summary)
                return 1

            summary = Text("Import complete.", style="bold green")
            summary.append(" ")
            summary.append_text(stats.line())
            console.print(summary)
            return 0
        finally:
            if source is not None:
                await source.close()
    finally:
        db.close()


def _maybe_traceback(console: Console, verbose: bool):
    if verbose:
        console.print_exception()


def main(
    output_path: Path,
    dcim_path: str = DCIM_PATH,
    db_path: Path | None = None,
    include_extensions: Sequence[str] = INCLUDE_EXTENSIONS,
    layout: str | None = None,
    force_layout: bool = False,
    udid: str | None = None,
    convert_heic: bool = False,
    skip_live_videos: bool = False,
    verbose: bool = False,
    concurrency: int = 4,
    download_retries: int = 2,
    since: datetime | None = None,
    until: datetime | None = None,
    manifest: Path | None = None,
    operation_timeout: float = DEFAULT_OPERATION_TIMEOUT,
    pair_timeout: float = DEFAULT_PAIR_TIMEOUT,
    legacy_timezone: ZoneInfo | None = None,
    legacy_fold: int | None = None,
):
    """Run the import and report progress on the console. Returns a process exit code
    (0 on success, 1 if the import failed or any file could not be downloaded)."""

    config = ImportConfig(
        output_path=output_path,
        dcim_path=dcim_path,
        db_path=db_path,
        include_extensions=include_extensions,
        layout=layout,
        force_layout=force_layout,
        udid=udid,
        convert_heic=convert_heic,
        skip_live_videos=skip_live_videos,
        verbose=verbose,
        concurrency=concurrency,
        download_retries=download_retries,
        operation_timeout=operation_timeout,
        pair_timeout=pair_timeout,
        since=since,
        until=until,
        manifest=manifest,
        legacy_timezone=legacy_timezone,
        legacy_fold=legacy_fold,
    )

    console = Console()

    try:
        exit_code = asyncio.run(_run_import(console, config))

    except MultipleDevicesError as e:
        console.print(Text("\nMore than one device is connected:", style="bold red"))

        for device_udid in e.udids:
            item = Text("  ")
            item.append(device_udid, style="cyan")
            console.print(item)

        console.print(
            "Pass [bold]--udid <UDID>[/bold] to pick the device to import from."
        )
        return 1

    except LayoutConflictError as e:
        message = Text("\nLayout conflict. ", style="bold red")
        message.append(str(e))
        message.append("\nPass ")
        message.append("--force", style="bold")
        message.append(
            " to switch this library to the new layout. Existing files keep their names."
        )
        console.print(message)
        return 1

    except (
        InvalidLayoutError,
        MissingHeicSupportError,
        LegacyTimezoneMigrationError,
    ) as e:
        message = Text("\nError: ", style="bold red")
        message.append(str(e))
        console.print(message)
        return 1

    except ConnectionError:
        _maybe_traceback(console, verbose)
        console.print(
            "\n[bold red]Could not connect to your device.[/]\n"
            " - Check the USB connection.\n"
            " - Make sure your device is unlocked and trusts this computer.\n"
            " - On Windows, make sure the iTunes or Apple Devices app is installed and running.\n"
        )
        console.print("[bold red]Import failed.[/]")
        return 1

    except KeyboardInterrupt:
        message = Text("\nImport cancelled.", style="bold yellow")
        message.append(
            " Finished downloads were kept. Run the same command to continue."
        )
        console.print(message)
        return 130

    except Exception as e:
        _maybe_traceback(console, verbose)
        message = Text("\nAn unexpected error occurred.", style="bold red")
        if verbose:
            message.append(" See the traceback above for details.")
        else:
            message.append(f" {e}\nRe-run with ")
            message.append("--verbose", style="bold")
            message.append(" for a full traceback.")
        console.print(message)
        console.print("[bold red]Import failed.[/]")
        return 1

    else:
        return exit_code
