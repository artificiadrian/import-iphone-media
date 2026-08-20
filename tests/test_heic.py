import asyncio
import io
import sqlite3

import pillow_heif
import pytest
from PIL import Image

import dcimport.heic as heic
from dcimport.db import MediaDatabase
from dcimport.importer import (
    FailedFile,
    NewFile,
    execute_import,
    plan_import,
    resolve_layout,
)
from tests.fake_source import DEFAULT_MTIME as MTIME
from tests.fake_source import FakeSource
from tests.helpers import run_import, scan

DATETIME_TAG = 0x0132


def make_heic_bytes(with_exif_datetime=None):
    pillow_heif.register_heif_opener()

    img = Image.new("RGB", (4, 4), "red")
    buf = io.BytesIO()

    if with_exif_datetime:
        exif = Image.Exif()
        exif[DATETIME_TAG] = with_exif_datetime
        img.save(buf, format="HEIF", exif=exif.tobytes())
    else:
        img.save(buf, format="HEIF")

    return buf.getvalue()


@pytest.fixture
def source():
    fake = FakeSource()
    fake.add("/DCIM/100APPLE/IMG_0001.HEIC", data=make_heic_bytes(), mtime=MTIME)
    return fake


def test_heic_is_converted_to_jpeg(source, tmp_path):
    results = run_import(source, tmp_path, convert_heic=True)

    target = tmp_path / "2024-01-02_03-04-05_IMG_0001.jpg"
    new = [r for r in results if isinstance(r, NewFile)]
    assert [n.local_path for n in new] == [target]

    with Image.open(target) as img:
        assert img.format == "JPEG"

    assert not list(tmp_path.glob("*.HEIC"))
    assert not list(tmp_path.glob("*.part"))


def test_converted_file_is_recorded_as_imported(source, tmp_path):
    run_import(source, tmp_path, convert_heic=True)
    plan = scan(source, tmp_path)

    assert not plan.to_download
    assert [p.afc_path.name for p in plan.existing] == ["IMG_0001.HEIC"]


def test_conversion_preserves_exif(tmp_path):
    source = FakeSource()
    source.add(
        "/DCIM/100APPLE/IMG_0001.HEIC",
        data=make_heic_bytes(with_exif_datetime="2024:01:02 03:04:05"),
        mtime=MTIME,
    )

    run_import(source, tmp_path, convert_heic=True)

    with Image.open(tmp_path / "2024-01-02_03-04-05_IMG_0001.jpg") as img:
        assert img.getexif()[DATETIME_TAG] == "2024:01:02 03:04:05"


def test_converted_file_keeps_iphone_mtime(source, tmp_path):
    run_import(source, tmp_path, convert_heic=True)

    target = tmp_path / "2024-01-02_03-04-05_IMG_0001.jpg"
    assert target.stat().st_mtime == MTIME.timestamp()


def test_heic_kept_as_is_without_flag(source, tmp_path):
    run_import(source, tmp_path)

    assert (tmp_path / "2024-01-02_03-04-05_IMG_0001.HEIC").exists()


def test_non_heic_files_are_not_converted(source, tmp_path):
    source.add("/DCIM/100APPLE/IMG_0002.JPG", data=b"jpegdata", mtime=MTIME)

    run_import(source, tmp_path, convert_heic=True)

    assert (tmp_path / "2024-01-02_03-04-05_IMG_0002.JPG").read_bytes() == b"jpegdata"


def test_missing_heic_support_raises(source, tmp_path, monkeypatch):
    monkeypatch.setattr(heic, "heic_support_available", lambda: False)

    with pytest.raises(heic.MissingHeicSupportError):
        run_import(source, tmp_path, convert_heic=True)


def test_conversion_does_not_follow_temporary_symlink(source, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"precious")
    temporary = tmp_path / "2024-01-02_03-04-05_IMG_0001.jpg.converted.part"
    temporary.symlink_to(outside)

    results = run_import(source, tmp_path, convert_heic=True)

    assert any(isinstance(result, NewFile) for result in results)
    assert outside.read_bytes() == b"precious"
    assert temporary.is_symlink()


def test_converted_file_recovers_after_database_finalize_failure(source, tmp_path):
    db = MediaDatabase(tmp_path / "media.db")
    layout = resolve_layout(db, None, False)
    complete_import = db.complete_import

    def fail_complete_import(afc_path, st_size, st_mtime):
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    db.complete_import = fail_complete_import

    async def collect():
        plan = await plan_import(source, db)
        first = [
            result
            async for result in execute_import(
                source, db, plan, tmp_path, layout, convert_heic=True
            )
        ]
        db.complete_import = complete_import
        recovered = await plan_import(source, db)
        return first, recovered

    try:
        first, recovered = asyncio.run(collect())
    finally:
        db.close()

    assert isinstance(first[0], FailedFile)
    assert not recovered.to_download
    assert not (tmp_path / "2024-01-02_03-04-05_IMG_0001_1.jpg").exists()
