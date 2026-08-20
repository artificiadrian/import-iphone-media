import json
import logging
import sqlite3
import sys
from datetime import datetime

import pytest

import dcimport.main as main_module
from dcimport.afc_utils import MultipleDevicesError
from dcimport.main import main
from tests.fake_source import DEFAULT_MTIME as MTIME
from tests.fake_source import FakeSource
from tests.helpers import persistently_fail


@pytest.fixture
def source(monkeypatch):
    fake = FakeSource()
    fake.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)

    async def connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(main_module, "afc_connect", connect)
    return fake


def test_main_imports_and_returns_zero(source, tmp_path):
    exit_code = main(tmp_path / "photos")

    assert exit_code == 0
    assert (
        tmp_path / "photos" / "2024-01-02_03-04-05_IMG_0001.JPG"
    ).read_bytes() == b"jpegdata"


def test_main_returns_one_when_downloads_fail(source, tmp_path):
    persistently_fail(source, "/DCIM/100APPLE/IMG_0001.JPG")

    exit_code = main(tmp_path / "photos")

    assert exit_code == 1


def test_multiple_devices_error_shows_udids_and_returns_one(
    monkeypatch, tmp_path, capsys
):
    async def connect(*args, **kwargs):
        raise MultipleDevicesError(udids=["00008101-AAAA", "00008101-BBBB"])

    monkeypatch.setattr(main_module, "afc_connect", connect)

    exit_code = main(tmp_path / "photos")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "00008101-AAAA" in out
    assert "--udid" in out


def test_device_name_is_printed(source, tmp_path, capsys):
    source.device_name = "Adrian's iPhone"

    main(tmp_path / "photos")

    assert "Adrian's iPhone" in capsys.readouterr().out


def test_layout_conflict_returns_one(source, tmp_path):
    main(tmp_path / "photos", layout="{mtime:%Y}/{name}")

    exit_code = main(tmp_path / "photos", layout="{name}")

    assert exit_code == 1


def test_invalid_layout_has_no_side_effects(monkeypatch, tmp_path):
    connected = False

    async def connect(*args, **kwargs):
        nonlocal connected
        connected = True
        return FakeSource()

    monkeypatch.setattr(main_module, "afc_connect", connect)
    output_path = tmp_path / "photos"

    exit_code = main(output_path, layout="{bogus}")

    assert exit_code == 1
    assert not connected
    assert not output_path.exists()


def test_already_up_to_date_returns_zero(source, tmp_path, capsys):
    main(tmp_path / "photos")

    exit_code = main(tmp_path / "photos")

    assert exit_code == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_concurrency_flag_limits_parallel_downloads(source, tmp_path):
    for i in range(5):
        source.add(f"/DCIM/100APPLE/IMG_010{i}.JPG")

    main(tmp_path / "photos", concurrency=1)

    assert source.max_concurrent_downloads == 1


def test_failed_files_are_listed(source, tmp_path, capsys):
    persistently_fail(source, "/DCIM/100APPLE/IMG_0001.JPG")

    exit_code = main(tmp_path / "photos")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "IMG_0001.JPG" in out
    assert "Failed files" in out
    assert "retry failed files" in out


def test_file_phrase_styles_the_complete_phrase():
    phrase = main_module._file_phrase(1, "new", "bold green")

    assert phrase.plain == "1 new file"
    assert phrase.style == "bold green"


def test_summary_uses_consistent_file_terms(source, tmp_path, capsys):
    main(tmp_path / "photos")

    output = capsys.readouterr().out
    assert "Found 1 new file" in output
    assert "0 existing files" in output
    assert "Import complete. Imported 1 new file; skipped 0 existing files." in output


def test_since_flag_skips_older_files(monkeypatch, tmp_path):
    fake = FakeSource()
    fake.add("/DCIM/100APPLE/OLD.JPG", data=b"old", mtime=datetime(2020, 1, 1))
    fake.add("/DCIM/100APPLE/NEW.JPG", data=b"new", mtime=datetime(2024, 6, 1))

    async def connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(main_module, "afc_connect", connect)
    out = tmp_path / "photos"

    main(out, since=datetime(2023, 1, 1))

    assert list(out.glob("*NEW.JPG"))
    assert not list(out.glob("*OLD.JPG"))


def test_manifest_records_imported_and_failed(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0002.JPG", data=b"data")
    persistently_fail(source, "/DCIM/100APPLE/IMG_0002.JPG")
    manifest = tmp_path / "report.json"

    main(tmp_path / "photos", manifest=manifest)

    data = json.loads(manifest.read_text())
    assert [r["afc_path"] for r in data["imported"]] == ["/DCIM/100APPLE/IMG_0001.JPG"]
    assert [r["afc_path"] for r in data["failed"]] == ["/DCIM/100APPLE/IMG_0002.JPG"]


def test_cli_accepts_legacy_timezone(source, tmp_path, monkeypatch):
    output_path = tmp_path / "photos"
    output_path.mkdir()
    conn = sqlite3.connect(output_path / "media.db")
    conn.execute(
        "CREATE TABLE media (afc_path TEXT NOT NULL, st_size INTEGER NOT NULL,"
        " st_mtime DATETIME NOT NULL, synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        " UNIQUE(afc_path, st_size, st_mtime))"
    )
    conn.execute(
        "INSERT INTO media (afc_path, st_size, st_mtime) VALUES (?, ?, ?)",
        ("/DCIM/100APPLE/OLD.JPG", 3, "2024-01-02T03:04:05"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["dcimport", "--legacy-timezone", "UTC", str(output_path)],
    )

    with pytest.raises(SystemExit) as exited:
        main_module.cli()

    assert exited.value.code == 0


def test_cli_rejects_legacy_fold_without_timezone(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["dcimport", "--legacy-fold", "earlier", str(tmp_path / "photos")],
    )

    with pytest.raises(SystemExit) as exited:
        main_module.cli()

    assert exited.value.code == 2
    assert "requires --legacy-timezone" in capsys.readouterr().err


def test_cli_forwards_device_timeouts(source, tmp_path, monkeypatch):
    received = {}

    async def connect(**kwargs):
        received.update(kwargs)
        return source

    monkeypatch.setattr(main_module, "afc_connect", connect)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dcimport",
            "--operation-timeout",
            "12.5",
            "--pair-timeout",
            "3",
            str(tmp_path / "photos"),
        ],
    )

    with pytest.raises(SystemExit) as exited:
        main_module.cli()

    assert exited.value.code == 0
    assert received == {"udid": None, "operation_timeout": 12.5, "pair_timeout": 3.0}


@pytest.mark.parametrize("timeout", ["0", "nan", "inf"])
def test_cli_rejects_invalid_operation_timeout(tmp_path, monkeypatch, capsys, timeout):
    monkeypatch.setattr(
        sys,
        "argv",
        ["dcimport", "--operation-timeout", timeout, str(tmp_path / "photos")],
    )

    with pytest.raises(SystemExit) as exited:
        main_module.cli()

    assert exited.value.code == 2
    assert "must be greater than zero" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("option", "value"), [("--concurrency", "0"), ("--retries", "-1")]
)
def test_cli_rejects_invalid_execution_limits(
    tmp_path, monkeypatch, capsys, option, value
):
    monkeypatch.setattr(
        sys, "argv", ["dcimport", option, value, str(tmp_path / "photos")]
    )

    with pytest.raises(SystemExit) as exited:
        main_module.cli()

    assert exited.value.code == 2
    assert "must" in capsys.readouterr().err


def test_cli_help_uses_named_groups(monkeypatch, capsys):
    parser = main_module._create_parser()
    monkeypatch.setattr(sys, "argv", ["dcimport", "--help"])

    with pytest.raises(SystemExit) as exited:
        main_module.cli()

    output = capsys.readouterr().out
    assert exited.value.code == 0
    assert "device options:" in output
    assert "file selection:" in output
    assert "diagnostics:" in output
    assert "\noptions:\n" not in output
    assert vars(parser)["color"] is False


@pytest.mark.parametrize(("verbose", "visible"), [(False, False), (True, True)])
def test_recovered_afc_warning_is_only_logged_when_verbose(
    monkeypatch, tmp_path, caplog, verbose, visible
):
    fake = FakeSource()
    fake.add("/DCIM/100APPLE/IMG_0001.JPG", data=b"jpegdata", mtime=MTIME)
    logger = logging.getLogger("pymobiledevice3.services.afc")

    async def connect(*args, **kwargs):
        logger.warning("AFC: no waiter for packet_num=%d", 17369)
        return fake

    monkeypatch.setattr(main_module, "afc_connect", connect)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        exit_code = main(tmp_path / "photos", verbose=verbose)

    messages = [record.getMessage() for record in caplog.records]
    assert exit_code == 0
    assert ("AFC: no waiter for packet_num=17369" in messages) is visible
