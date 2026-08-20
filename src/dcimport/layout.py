"""Filename layout templates: `str.format`-style strings with `{name}` (original
filename) and `{mtime:...}` (photo modification time, strftime codes) placeholders.
The rendered result is a path relative to the output directory and may contain
subdirectories, e.g. `{mtime:%Y}/{mtime:%m}/{name}`."""

import os
import string
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath

from dcimport.immutable import immutable

DEFAULT_LAYOUT = "{mtime:%Y-%m-%d_%H-%M-%S}_{name}"

_ALLOWED_FIELDS = ("name", "mtime")
_VALIDATION_MTIME = datetime(2000, 1, 2, 3, 4, 5)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class InvalidLayoutError(ValueError):
    """The layout template is malformed; the message says why."""


@immutable
class Layout:
    """A validated layout template. Obtain via `parse_layout`."""

    template: str

    def render(self, name: str, mtime: datetime) -> PurePosixPath:
        """Render the relative target path for a file called `name` modified at `mtime`."""

        path = PurePosixPath(self.template.format(name=name, mtime=mtime))
        _validate_rendered_path(path)

        return path


def _validate_rendered_path(
    path: PurePosixPath,
    *,
    windows: bool | None = None,
) -> None:
    rendered = str(path)
    windows_path = PureWindowsPath(rendered)

    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.root
        or windows_path.drive
    ):
        msg = f"Rendered layout '{rendered}' must be a relative path"
        raise InvalidLayoutError(msg)

    if ".." in path.parts or ".." in windows_path.parts:
        msg = f"Rendered layout '{rendered}' must not contain '..' segments"
        raise InvalidLayoutError(msg)

    if "\\" in rendered:
        msg = f"Rendered layout '{rendered}' must not contain backslashes"
        raise InvalidLayoutError(msg)

    validate_windows = os.name == "nt" if windows is None else windows
    if validate_windows:
        for part in windows_path.parts:
            invalid_character = any(
                character in '<>:"|?*' or ord(character) < 32 for character in part
            )
            reserved_name = part.rstrip(" .").split(".", maxsplit=1)[0].upper()

            if (
                invalid_character
                or part.endswith((" ", "."))
                or reserved_name in _WINDOWS_RESERVED_NAMES
            ):
                msg = f"Rendered layout '{rendered}' is not a valid Windows path"
                raise InvalidLayoutError(msg)


def parse_layout(template: str) -> Layout:
    """Parse and validate a layout template.

    Raises:
        InvalidLayoutError: If the template has unknown placeholders, lacks `{name}`,
            is not a relative path, or contains `..` segments."""

    try:
        fields = [
            field
            for _, field, _, _ in string.Formatter().parse(template)
            if field is not None
        ]
    except ValueError as e:
        msg = f"Malformed layout template '{template}': {e}"
        raise InvalidLayoutError(msg) from e

    unknown = [f for f in fields if f not in _ALLOWED_FIELDS]

    if unknown:
        msg = f"Unknown placeholder(s) {', '.join(unknown)} in layout '{template}'; the allowed placeholders are {{name}} and {{mtime:...}}"
        raise InvalidLayoutError(msg)

    if "name" not in fields:
        msg = f"Layout '{template}' must contain the {{name}} placeholder"
        raise InvalidLayoutError(msg)

    path = PurePosixPath(template)

    if path.is_absolute():
        msg = f"Layout '{template}' must be a relative path"
        raise InvalidLayoutError(msg)

    if ".." in path.parts:
        msg = f"Layout '{template}' must not contain '..' segments"
        raise InvalidLayoutError(msg)

    layout = Layout(template)
    layout.render(name="example.jpg", mtime=_VALIDATION_MTIME)
    return layout
