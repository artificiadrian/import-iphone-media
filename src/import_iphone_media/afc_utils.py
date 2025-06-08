import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from time import sleep
from typing import cast

from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import MAXIMUM_READ_SIZE, AfcService


def afc_download(afc: AfcService, afc_path: PurePosixPath, local_path: Path):
    """Download a file from iPhone and update its modification time to match the iPhone's.

    Args:
        afc (AfcService): The AFC service instance connected to the iPhone.
        afc_path (Path): The path of the file on the iPhone to download.
        local_path (Path): The local path where the file should be saved.
    """

    handle = afc.fopen(str(afc_path), "r")

    # download file
    with open(local_path, "wb") as local_file:
        while True:
            data = afc.fread(handle, MAXIMUM_READ_SIZE)  # type: ignore[reportTypeArgument] # pymobiledevice3 bug?

            if not data:
                break

            local_file.write(data)

    afc.fclose(handle)

    # set file modification time to the same as on the iPhone
    stat = cast(dict, afc.stat(str(afc_path)))

    mtime = cast(datetime, stat["st_mtime"]).timestamp()
    atime = datetime.now().timestamp()

    os.utime(local_path, (atime, mtime))


def afc_is_dir(stat: dict):
    """Check if the file info is a directory.

    Args:
        stat (dict): The file info dictionary returned by afc.stat().

    Returns:
        bool: True if the file is a directory, False otherwise.
    """
    return (
        stat.get("st_ifmt") == "S_IFDIR"
    )  # source: https://github.com/doronz88/pymobiledevice3/blob/master/pymobiledevice3/services/afc.py:422


def afc_dirlist(afc: AfcService, afc_path: PurePosixPath):
    """List the contents of a directory on the iPhone.

    Args:
        afc (AfcService): The AFC service instance connected to the iPhone.
        afc_path (str): The path of the directory on the iPhone.

    Returns:
        list: A list of dictionaries containing file info for each item in the directory.
    """

    for path in afc.dirlist(str(afc_path)):
        yield PurePosixPath(path)


def afc_stat(afc: AfcService, afc_path: PurePosixPath) -> dict:
    """Get the file info for a file or directory on the iPhone.

    Args:
        afc (AfcService): The AFC service instance connected to the iPhone.
        afc_path (str): The path of the file or directory on the iPhone.

    Returns:
        dict: A dictionary containing file info.
    """

    return afc.stat(str(afc_path))


def afc_connect(retries: int = 3):
    """Connect to the iPhone using AFC service.
    Args:
        retries (int): Number of retries to connect to the iPhone.
    Returns:
        AfcService: An instance of the AFC service connected to the iPhone.
    Raises:
        RuntimeError: If the connection fails after the specified number of retries.
    """

    _e = None

    for i in range(retries):
        try:
            return AfcService(create_using_usbmux(autopair=True))

        except ConnectionFailedToUsbmuxdError as e:
            _e = e
            sleep(2**i)  # exponential backoff

        except Exception as e:
            raise ConnectionError("Failed to connect to iPhone due to an unknown error") from e

    raise ConnectionError(f"Failed to connect to iPhone after {retries} retries") from _e
