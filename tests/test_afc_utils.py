import asyncio
from pathlib import PurePosixPath
from typing import cast

import pytest
from pymobiledevice3.services.afc import AfcService
from typing_extensions import override

import dcimport.afc_utils as afc_utils


class FakeLockdown:
    def __init__(self):
        self.display_name = "Test iPhone"
        self.closed = False

    async def close(self):
        self.closed = True

    async def get_value(self, *, key):
        assert key == "DeviceName"
        return "Test iPhone"


class FakeAfc:
    def __init__(self, enter_error=None, exit_error=None):
        self.enter_error = enter_error
        self.exit_error = exit_error
        self.closed = False

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *_):
        self.closed = True
        if self.exit_error is not None:
            raise self.exit_error

    async def stat(self, _) -> dict:
        await asyncio.Event().wait()
        return {}


class ReadableAfc(FakeAfc):
    def __init__(self):
        super().__init__()
        self.closed_handle = False
        self._chunks = iter((b"jpg", b""))

    async def fopen(self, path, mode):
        assert path == "/DCIM/IMG_0001.JPG"
        assert mode == "r"
        return 1

    async def fread(self, handle, size):
        assert handle == 1
        assert size == afc_utils.MAXIMUM_READ_SIZE
        return next(self._chunks)

    async def fclose(self, handle):
        assert handle == 1
        self.closed_handle = True


class CancellationAfc(FakeAfc):
    def __init__(self):
        super().__init__()
        self.read_started = asyncio.Event()

    async def fopen(self, *_):
        return 1

    async def fread(self, *_):
        self.read_started.set()
        await asyncio.Event().wait()

    async def fclose(self, *_):
        msg = "close failed"
        raise OSError(msg)


class ListingAfc(FakeAfc):
    def __init__(self, delays):
        super().__init__()
        self.delays = iter(delays)

    async def listdir(self, _):
        await asyncio.sleep(next(self.delays))
        return ["IMG_0000.JPG", "IMG_0001.JPG", "IMG_0002.JPG"]

    @override
    async def stat(self, _):
        await asyncio.sleep(next(self.delays))
        return {"st_ifmt": "S_IFREG"}


@pytest.mark.parametrize("operation_timeout", [float("nan"), float("inf")])
def test_source_rejects_nonfinite_operation_timeout(operation_timeout):
    with pytest.raises(ValueError, match="operation_timeout"):
        afc_utils.AfcSource(
            cast(AfcService, FakeAfc()),
            lockdown=FakeLockdown(),
            operation_timeout=operation_timeout,
        )


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("operation_timeout", float("nan")),
        ("operation_timeout", float("inf")),
        ("pair_timeout", float("nan")),
        ("pair_timeout", float("inf")),
    ],
)
def test_connect_rejects_nonfinite_timeouts(monkeypatch, option, value):
    async def connect_once(*_, **__):
        return object()

    monkeypatch.setattr(afc_utils, "_connect_once", connect_once)

    with pytest.raises(ValueError, match=option):
        asyncio.run(afc_utils.afc_connect(**{option: value}))


def test_close_releases_afc_and_lockdown():
    afc = FakeAfc()
    lockdown = FakeLockdown()
    source = afc_utils.AfcSource(
        cast(AfcService, afc), lockdown=lockdown, operation_timeout=1
    )

    asyncio.run(source.close())

    assert afc.closed
    assert lockdown.closed


def test_source_preserves_previous_positional_device_name():
    source = afc_utils.AfcSource(cast(AfcService, FakeAfc()), "Test iPhone")

    assert source.device_name == "Test iPhone"
    asyncio.run(source.close())


def test_close_releases_lockdown_when_afc_close_fails():
    afc = FakeAfc(exit_error=OSError("afc close failed"))
    lockdown = FakeLockdown()
    source = afc_utils.AfcSource(
        cast(AfcService, afc), lockdown=lockdown, operation_timeout=1
    )

    with pytest.raises(OSError, match="afc close failed"):
        asyncio.run(source.close())

    assert lockdown.closed


def test_connect_forwards_pair_timeout(monkeypatch):
    afc = FakeAfc()
    lockdown = FakeLockdown()
    received = {}

    async def create_using_usbmux(**kwargs):
        received.update(kwargs)
        return lockdown

    monkeypatch.setattr(afc_utils, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(afc_utils, "AfcService", lambda _: afc)

    source = asyncio.run(
        afc_utils._connect_once("00008101-TEST", operation_timeout=5, pair_timeout=7)
    )

    assert received == {
        "serial": "00008101-TEST",
        "autopair": True,
        "pair_timeout": 7,
    }
    asyncio.run(source.close())


def test_connect_closes_lockdown_when_afc_setup_fails(monkeypatch):
    afc = FakeAfc(enter_error=OSError("afc setup failed"))
    lockdown = FakeLockdown()

    async def create_using_usbmux(**kwargs):
        return lockdown

    monkeypatch.setattr(afc_utils, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(afc_utils, "AfcService", lambda _: afc)

    with pytest.raises(OSError, match="afc setup failed"):
        asyncio.run(
            afc_utils._connect_once(
                "00008101-TEST", operation_timeout=5, pair_timeout=7
            )
        )

    assert lockdown.closed


def test_stat_times_out_when_afc_does_not_respond():
    source = afc_utils.AfcSource(
        cast(AfcService, FakeAfc()),
        lockdown=FakeLockdown(),
        operation_timeout=0.1,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(source.stat(PurePosixPath("/DCIM/IMG_0001.JPG")))


def test_download_closes_afc_handle(tmp_path):
    afc = ReadableAfc()
    source = afc_utils.AfcSource(
        cast(AfcService, afc), lockdown=FakeLockdown(), operation_timeout=1
    )
    target = tmp_path / "IMG_0001.JPG"

    with target.open("w+b") as output:
        asyncio.run(source.download(PurePosixPath("/DCIM/IMG_0001.JPG"), output))

    assert target.read_bytes() == b"jpg"
    assert afc.closed_handle


def test_download_cleanup_does_not_mask_cancellation(tmp_path):
    afc = CancellationAfc()
    source = afc_utils.AfcSource(
        cast(AfcService, afc), lockdown=FakeLockdown(), operation_timeout=1
    )

    async def cancel_download():
        with (tmp_path / "IMG_0001.JPG").open("w+b") as output:
            task = asyncio.create_task(
                source.download(PurePosixPath("/DCIM/IMG_0001.JPG"), output)
            )
            await afc.read_started.wait()
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(cancel_download())


def test_listing_timeout_applies_to_each_entry():
    source = afc_utils.AfcSource(
        cast(AfcService, ListingAfc([0.01, 0.01, 0.01, 0.01])),
        lockdown=FakeLockdown(),
        operation_timeout=0.1,
    )

    async def list_files():
        return [path async for path in source.list_files(PurePosixPath("/DCIM"))]

    assert len(asyncio.run(list_files())) == 4


def test_listing_times_out_when_next_entry_stalls():
    source = afc_utils.AfcSource(
        cast(AfcService, ListingAfc([0.01, 0.5])),
        lockdown=FakeLockdown(),
        operation_timeout=0.1,
    )

    async def list_files():
        return [path async for path in source.list_files(PurePosixPath("/DCIM"))]

    with pytest.raises(TimeoutError):
        asyncio.run(list_files())


def test_pair_timeout_is_not_capped_by_operation_timeout(monkeypatch):
    afc = FakeAfc()
    lockdown = FakeLockdown()

    async def create_using_usbmux(**kwargs):
        await asyncio.sleep(0.02)
        return lockdown

    monkeypatch.setattr(afc_utils, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(afc_utils, "AfcService", lambda _: afc)

    source = asyncio.run(
        afc_utils._connect_once(
            "00008101-TEST", operation_timeout=0.01, pair_timeout=0.1
        )
    )

    asyncio.run(source.close())
