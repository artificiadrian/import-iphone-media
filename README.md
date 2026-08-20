# dcimport

[![PyPI](https://img.shields.io/pypi/v/dcimport.svg)](https://pypi.org/project/dcimport/) ![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-blue) [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

`dcimport` copies photos and videos from an iPhone to a local folder over USB. It reads directly from the device without iCloud and installs nothing on the phone.

Imports are incremental. A local database tracks completed files, so later runs copy only new or changed media. Interrupted imports continue when you run the same command again.

<img src="https://raw.githubusercontent.com/artificiadrian/dcimport/main/.assets/demo.webp" alt="dcimport importing media from an iPhone" width="900">

## Installation

Requires Python 3.11+.

```sh
uv tool install dcimport
# or: pip install dcimport
```

Install the optional HEIC converter with `uv tool install "dcimport[heic]"`.

## Quick start

Connect the iPhone by USB, unlock it, and tap **Trust**. Then choose the destination folder:

```sh
dcimport ~/Pictures/iPhone
```

On Windows, install [Apple Devices or iTunes](https://support.apple.com/en-us/HT210384) for the device drivers.

Useful examples:

```sh
dcimport ~/Pictures --layout "{mtime:%Y}/{mtime:%m}/{name}"
dcimport ~/Pictures --since 2024-01-01 --convert-heic
dcimport ~/Pictures --skip-live-videos
dcimport ~/Pictures --manifest import.json
```

Run `dcimport --help` for the full list of options.

## File names and folders

`--layout` accepts `{name}` and `{mtime:...}`. The latter uses [strftime](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes) codes for the file modification time. Slashes create subfolders.

The default layout is `{mtime:%Y-%m-%d_%H-%M-%S}_{name}`. The selected layout is saved for that destination. Use `--force` to change it later.

## Import tracking

`media.db` in the destination records each device path, size, and modification time. Editing media on the phone changes its identity and imports it again. Existing local files are not overwritten; name conflicts receive `_1`, `_2`, and later suffixes.

Delete `media.db` only if you intentionally want the next run to import every file again.

Databases with timezone-less records from versions before 0.2 require `--legacy-timezone ZONE`. If a timestamp falls in a repeated daylight-saving hour, also pass `--legacy-fold earlier` or `--legacy-fold later`.
