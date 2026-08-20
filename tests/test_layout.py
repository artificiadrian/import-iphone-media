from datetime import datetime
from pathlib import PurePosixPath

import pytest

from dcimport.layout import (
    DEFAULT_LAYOUT,
    InvalidLayoutError,
    _validate_rendered_path,
    parse_layout,
)

MTIME = datetime(2024, 1, 2, 3, 4, 5)


def test_default_layout_renders_timestamped_name():
    layout = parse_layout(DEFAULT_LAYOUT)

    rendered = layout.render(name="IMG_0001.JPG", mtime=MTIME)

    assert str(rendered) == "2024-01-02_03-04-05_IMG_0001.JPG"


def test_layout_with_subdirectories():
    layout = parse_layout("{mtime:%Y}/{mtime:%m}/{name}")

    rendered = layout.render(name="IMG_0001.JPG", mtime=MTIME)

    assert rendered.parts == ("2024", "01", "IMG_0001.JPG")


def test_layout_without_name_placeholder_is_rejected():
    with pytest.raises(InvalidLayoutError, match=r"\{name\}"):
        parse_layout("{mtime:%Y}/photos")


def test_layout_with_unknown_placeholder_is_rejected():
    with pytest.raises(InvalidLayoutError, match="foo"):
        parse_layout("{foo}_{name}")


def test_absolute_layout_is_rejected():
    with pytest.raises(InvalidLayoutError, match="relative"):
        parse_layout("/photos/{name}")


def test_parent_traversal_is_rejected():
    with pytest.raises(InvalidLayoutError, match=r"\.\."):
        parse_layout("../{name}")


def test_absolute_mtime_format_is_rejected_during_parsing():
    with pytest.raises(InvalidLayoutError, match="relative"):
        parse_layout("{mtime:/tmp/}{name}")


def test_windows_drive_relative_layout_is_rejected_during_parsing():
    with pytest.raises(InvalidLayoutError, match="relative"):
        parse_layout("C:{name}")


@pytest.mark.parametrize("name", ["CON.jpg", "bad:name.jpg", "trailing. "])
def test_windows_invalid_names_are_rejected(name):
    with pytest.raises(InvalidLayoutError, match="Windows"):
        _validate_rendered_path(PurePosixPath(name), windows=True)
