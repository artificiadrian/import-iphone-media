import asyncio
import math
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import PurePosixPath
from typing import BinaryIO, Protocol, cast

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import MAXIMUM_READ_SIZE, AfcService

from dcimport.importer import DirEntry, FileStat

DEFAULT_OPERATION_TIMEOUT = 60.0
DEFAULT_PAIR_TIMEOUT = 30.0


def _require_finite_positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        msg = f"{name} must be finite and greater than zero"
        raise ValueError(msg)


class _LockdownConnection(Protocol):
    async def close(self) -> None: ...


class MultipleDevicesError(Exception):
    """More than one device is connected and no UDID was given to pick one."""

    def __init__(self, udids: list[str]):
        super().__init__(f"Multiple devices connected: {', '.join(udids)}")
        self.udids = udids


class AfcSource:
    """MediaSource implementation backed by an AFC connection to an iPhone.
    Obtain via `afc_connect`; call `close` when done."""

    def __init__(
        self,
        afc: AfcService,
        device_name: str | None = None,
        *,
        lockdown: _LockdownConnection | None = None,
        operation_timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ):
        _require_finite_positive(operation_timeout, "operation_timeout")

        self._afc = afc
        self._lockdown = lockdown
        self._operation_timeout = operation_timeout
        self.device_name = device_name

    async def list_files(self, path: PurePosixPath) -> AsyncIterator[PurePosixPath]:
        """Recursively list `path` with a deadline on each AFC request."""

        root = str(path)
        pending_directories = [root]
        yield path

        while pending_directories:
            directory = pending_directories.pop()
            async with asyncio.timeout(self._operation_timeout):
                names = await self._afc.listdir(directory)

            directories = []
            files = []

            for name in names:
                entry = str(PurePosixPath(directory) / name)
                async with asyncio.timeout(self._operation_timeout):
                    metadata = await self._afc.stat(entry)

                if metadata.get("st_ifmt") == "S_IFDIR":
                    directories.append(entry)
                else:
                    files.append(entry)

            for entry in directories + files:
                yield PurePosixPath(entry)

            pending_directories.extend(reversed(directories))

    async def stat(self, path: PurePosixPath) -> FileStat | DirEntry:
        """Stat `path` via AFC, mapping a directory to `DirEntry`."""

        async with asyncio.timeout(self._operation_timeout):
            raw = await self._afc.stat(str(path))

        if raw.get("st_ifmt") == "S_IFDIR":
            return DirEntry()

        return FileStat(
            size=cast(int, raw["st_size"]),
            mtime=cast(datetime, raw["st_mtime"]),
        )

    async def download(self, path: PurePosixPath, target: BinaryIO) -> None:
        """Stream `path` off the device in chunks into local `target`."""

        async with asyncio.timeout(self._operation_timeout):
            handle = await self._afc.fopen(str(path), "r")

        download_error = None

        try:
            while True:
                async with asyncio.timeout(self._operation_timeout):
                    data = await self._afc.fread(handle, MAXIMUM_READ_SIZE)

                if not data:
                    break

                target.write(data)
        except BaseException as e:
            download_error = e
            raise
        finally:
            try:
                async with asyncio.timeout(self._operation_timeout):
                    await self._afc.fclose(handle)
            except BaseException:
                if download_error is None:
                    raise

    async def close(self):
        try:
            async with asyncio.timeout(self._operation_timeout):
                await self._afc.__aexit__(None, None, None)
        finally:
            if self._lockdown is not None:
                async with asyncio.timeout(self._operation_timeout):
                    await self._lockdown.close()


async def afc_connect(
    udid: str | None = None,
    retries: int = 3,
    operation_timeout: float = DEFAULT_OPERATION_TIMEOUT,
    pair_timeout: float = DEFAULT_PAIR_TIMEOUT,
) -> AfcSource:
    """Connect to an iPhone and return an AfcSource.

    With multiple devices connected, `udid` selects which one; without it, exactly
    one device must be connected. Pairing and AFC requests have separate deadlines.

    Raises:
        MultipleDevicesError: If several devices are connected and `udid` is None.
        ConnectionError: If the connection fails after `retries` attempts.
        ValueError: If a retry count or deadline is not positive.
    """

    if retries <= 0:
        msg = "retries must be greater than zero"
        raise ValueError(msg)

    _require_finite_positive(operation_timeout, "operation_timeout")
    _require_finite_positive(pair_timeout, "pair_timeout")

    last_error = None

    for attempt in range(retries):
        try:
            source = await _connect_once(
                udid,
                operation_timeout=operation_timeout,
                pair_timeout=pair_timeout,
            )
        except MultipleDevicesError:
            raise
        except (ConnectionFailedToUsbmuxdError, TimeoutError) as e:
            last_error = e
            await asyncio.sleep(2**attempt)
        except Exception as e:
            msg = "Failed to connect to the device due to an unexpected error"
            raise ConnectionError(msg) from e
        else:
            return source

    msg = f"Failed to connect to the device after {retries} attempts"
    raise ConnectionError(msg) from last_error


async def _connect_once(
    udid: str | None,
    *,
    operation_timeout: float,
    pair_timeout: float,
) -> AfcSource:
    if udid is None:
        async with asyncio.timeout(operation_timeout):
            devices = await usbmux.list_devices()
        serials = sorted({device.serial for device in devices})

        if len(serials) > 1:
            raise MultipleDevicesError(udids=serials)

    # create_using_usbmux owns the trust-prompt deadline and cleans up ordinary
    # failures. Do not cancel it with the shorter AFC operation timeout.
    lockdown = await create_using_usbmux(
        serial=udid,
        autopair=True,
        pair_timeout=pair_timeout,
    )

    try:
        async with asyncio.timeout(operation_timeout):
            raw_name = await lockdown.get_value(key="DeviceName")
            afc = AfcService(lockdown)
            await afc.__aenter__()
    except BaseException:
        try:
            async with asyncio.timeout(operation_timeout):
                await lockdown.close()
        except BaseException:
            pass
        raise

    device_name = raw_name if isinstance(raw_name, str) else lockdown.display_name
    return AfcSource(
        afc,
        device_name=device_name,
        lockdown=lockdown,
        operation_timeout=operation_timeout,
    )
