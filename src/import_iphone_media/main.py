from pathlib import Path
from typing import Optional

from import_iphone_media.importer import (
    DCIM_PATH,
    INCLUDE_EXTENSIONS,
    ExistingFile,
    NewFile,
    import_media_files,
)


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="Import media files from iPhone")

    parser.add_argument("output", help="Directory where media files should be downloaded to", type=Path)

    parser.add_argument(
        "--dcim-path",
        help="Directory on iPhone to scan for media files",
        type=str,
        default=DCIM_PATH,
    )

    parser.add_argument(
        "--db-path",
        help="Path to the database file where information on imported media will be stored",
        type=Path,
        default=None,
    )

    parser.add_argument(
        "--include-extensions",
        help="List of file extensions to include (comma-separated)",
        type=str,
        default=",".join(INCLUDE_EXTENSIONS),
    )

    parser.add_argument(
        "--verbose",
        help="Enable verbose output",
        action="store_true",
    )

    return parser.parse_args()


def cli():
    args = _parse_args()

    include_extensions = [ext.strip() for ext in args.include_extensions.split(",")]

    main(
        output_path=Path(args.output),
        dcim_path=args.dcim_path,
        db_path=args.db_path,
        include_extensions=include_extensions,
        verbose=args.verbose,
    )


def main(
    output_path: Path,
    dcim_path: str = DCIM_PATH,
    db_path: Optional[Path] = None,
    include_extensions: list[str] = INCLUDE_EXTENSIONS,
    verbose: bool = False,
):
    from rich.console import Console

    console = Console()
    n_new, n_existing = 0, 0

    def _fancy_stats():
        return f"Imported [bold][green]{n_new}[/bold] new[/green] and skipped [bold][yellow]{n_existing}[/bold] existing[/yellow] files"

    if not output_path.exists():
        console.print(f"[red]Output path '{output_path}' does not exist.[/red]")
        return

    console.print(
        f"Importing media files from [blue]'{dcim_path}'[/blue] on your iPhone to [blue]'{output_path.absolute()}'[/blue]"
    )

    try:
        with console.status("Connecting to iPhone...") as status:
            for file in import_media_files(
                output_path,
                str(dcim_path),
                db_path,
                include_extensions,
            ):
                if isinstance(file, NewFile):
                    n_new += 1
                elif isinstance(file, ExistingFile):
                    n_existing += 1

                if verbose:
                    console.print(
                        f"{'Downloaded new' if isinstance(file, NewFile) else 'Skipped existing' if isinstance(file, ExistingFile) else 'Ignored'} file '{file.afc_path}'"
                    )

                color = "green" if isinstance(file, NewFile) else "yellow"

                status.update(f"{_fancy_stats()}. ([{color}]'{file.afc_path}'[/{color}])")

        console.print(f"[bold green]Import completed![/] {_fancy_stats()}")

    except ConnectionError:
        if verbose:
            console.print_exception()

        console.print(
            "\n[red]An error occurred while connecting to your iPhone.[/red]\n - If you are using Windows, please ensure that the iTunes/Apple Devices app is installed and running.\n - Check the USB connection to your iPhone.\n - Ensure your iPhone is unlocked and trusted.\n"
        )
        console.print(f"[bold red]Import failed![/] {_fancy_stats()}")

    except Exception as e:
        if verbose:
            console.print_exception()

        hint = "Check the error message above for more details." if verbose else f"{e}"

        console.print(f"\n[red]An unexpected error occurred while importing media files. {hint}[/red]\n")
        console.print(f"[bold red]Import failed![/] {_fancy_stats()}")

    except KeyboardInterrupt:
        console.print(f"[bold red]Import cancelled by user![/] {_fancy_stats()}")
