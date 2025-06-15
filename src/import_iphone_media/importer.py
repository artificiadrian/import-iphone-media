from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional, cast

from pydantic import BaseModel

from import_iphone_media.afc_utils import afc_connect, afc_dirlist, afc_download, afc_is_dir, afc_stat
from import_iphone_media.db import MediaDatabase

DCIM_PATH = "/DCIM"
INCLUDE_EXTENSIONS = ["jpg", "jpeg", "png", "mov", "mp4", "heic"]
FILENAME_DATETIME_FORMAT = "%Y-%m-%d_%H-%M-%S"
DEFAULT_DB_NAME = "media.db"


class NewFile(BaseModel):
    """Represents a new media file that has been downloaded from the iPhone."""

    afc_path: PurePosixPath
    local_path: Path
    stat: dict


class ExistingFile(BaseModel):
    """Represents a media file that already exists in the database."""

    afc_path: PurePosixPath
    stat: dict


class IgnoredFile(BaseModel):
    """Represents a media file that was ignored due to its extension or being a directory."""

    afc_path: PurePosixPath


def import_media_files(
    output_path: Path,
    dcim_path: str = DCIM_PATH,
    db_path: Optional[Path] = None,
    include_extensions: list[str] = INCLUDE_EXTENSIONS,
    connection_retries: int = 3,
):
    """Import media files from an iPhone's DCIM directory.

    Args:
        output_path (Path): Directory where media files should be downloaded to.
        dcim_path (str): Directory on iPhone to scan for media files.
        db_path (Optional[Path]): Path to the database file where information on imported media will be stored.
        include_extensions (list[str]): List of file extensions to include in the import.
        connection_retries (int): Number of retries to connect to the iPhone.

    Yields:
        Union[NewFile,ExistingFile,IgnoredFile]: NewFile if a new media file is downloaded, ExistingFile if a media file already exists in the database, IgnoredFile if a file is ignored due to its extension or being a directory.

    Raises:
        ConnectionError: If the connection to the iPhone fails after the specified number of retries (or due to an unknown error)."""

    if not output_path.exists():
        raise FileNotFoundError(f"Output path '{output_path}' does not exist.")

    afc = afc_connect(retries=connection_retries)
    db = MediaDatabase(db_path or output_path / DEFAULT_DB_NAME)

    try:
        for path in afc_dirlist(afc, PurePosixPath(dcim_path)):
            if path.suffix.lower()[1:] not in include_extensions:
                # ignore files with extensions not in the include list
                yield IgnoredFile(afc_path=path)
                continue

            stat = afc_stat(afc, path)

            if afc_is_dir(stat):
                # ignore directories
                yield IgnoredFile(afc_path=path)
                continue

            st_size = cast(int, stat["st_size"])
            st_mtime = cast(datetime, stat["st_mtime"])

            if not db.try_insert(path, st_size, st_mtime):
                # file already exists in the database, skip it
                yield ExistingFile(afc_path=path, stat=stat)
                continue

            # file is new, download it
            target_path = output_path / f"{st_mtime.strftime(FILENAME_DATETIME_FORMAT)}_{path.name}"
            afc_download(afc, path, target_path)

            yield NewFile(afc_path=path, local_path=target_path, stat=stat)

    finally:
        db.close()
        afc.close()
