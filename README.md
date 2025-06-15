# import-iphone-media

[![PyPI](https://img.shields.io/pypi/v/import-iphone-media.svg)](https://pypi.org/project/import-iphone-media/) ![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-blue) [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Command-line tool for direct USB import of photos and videos from iPhone. Preserves original timestamps, skips duplicates, and uses a database for tracking. No iCloud, iTunes sync, or deletion.

![test](.assets/usage.gif)

_This project is not affiliated with or endorsed by Apple Inc. ‘iPhone’ is a trademark of Apple Inc._

## Quick Start

1. Connect iPhone via USB.  
   (On Windows: [iTunes or the Apple Devices app](https://support.apple.com/en-us/HT210384) must be installed and running.)
2. Unlock iPhone and confirm "Trust" if prompted.
3. Install:

   ```sh
   # Using uv (recommended)
   uv tool install import-iphone-media

   # Using pip
   pip install import-iphone-media
   ```

4. Import media:
   ```sh
   import-iphone-media ~/Pictures
   ```

## Features

- USB transfer; no iCloud/internet needed.
- Preserves file timestamps.
- Skips files already imported (uses DB).
- Filenames: `YYYY-MM-DD_HH-MM-SS_ORIGINALNAME.JPG`
- Cross-platform: Windows, macOS, Linux.
- Read-only: does not delete or modify on iPhone.

## Installation

**Requirements**

- Python 3.9+
- iPhone & USB cable
- Windows: iTunes/Apple Devices app running (for drivers)

**Install**

```sh
# Using uv (recommended)
uv tool install import-iphone-media

# Or pip
pip install import-iphone-media
```

## Usage

Run `import-iphone-media` with the destination folder.

```sh
import-iphone-media ~/Pictures/iPhoneBackup
```

Options (see `-h`):

```sh
usage: import-iphone-media [-h] [--dcim-path DCIM_PATH] [--db-path DB_PATH]
[--include-extensions EXT1,EXT2,...] [--verbose]
output

output Destination directory

Options:
-h, --help Show help
--dcim-path DCIM_PATH iPhone directory to scan (default: /DCIM)
--db-path DB_PATH Path to database (default: media.db in output)
--include-extensions EXT1,EXT2,...
Comma-separated file extensions (default: jpg,jpeg,png,mov,mp4,heic)
--verbose Verbose output
```

## How It Stores Files

- Files are saved in the specified directory with filenames:  
  `YYYY-MM-DD_HH-MM-SS_ORIGINALNAME.JPG`
- `media.db` tracks imported files and avoids duplicates.
- No files are deleted or modified on the iPhone.

## Troubleshooting

- **No device found:** iPhone must be unlocked and trusted.
- **Windows:** Ensure Apple software is running for drivers.
- **Reconnect:** Try unplug/replug or different USB port.
- **Restart:** Restart iPhone or computer if persistent issues.
- **Linux:** If permission errors, use `sudo` or set udev rules.

## Development

1. Clone:

   ```sh
   git clone https://github.com/artificiadrian/import-iphone-media.git
   ```

2. Install deps:

   ```sh
   cd import-iphone-media
   uv sync
   ```

## Acknowledgements

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)
- [rich](https://github.com/Textualize/rich)
