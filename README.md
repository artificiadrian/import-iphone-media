# import-iphone-media

A fast, efficient CLI tool that imports photos and videos directly from your iPhone to your computer via USB. Preserves original timestamps, skips duplicates, and works without iCloud or iTunes sync.

## Quick Start

1. Connect your iPhone via USB
   - (If on **Windows**: Make sure [iTunes/Apple Devices app](https://support.apple.com/en-us/HT210384) is installed and running.)
2. Unlock your iPhone and trust your computer if prompted.
3. Install and run:

```sh
uv tool install import-iphone-media  # Recommended
pip install import-iphone-media      # Alternative

import-iphone-media ~/Pictures/iPhone
```

## Table of Contents

- [Quick Start](#quick-start)
- [Features](#features)
- [Installation](#installation)
- [Usage](#usage)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Acknowledgements](#acknowledgements)

## Features

- Connects to your iPhone via USB (no iCloud required)
- Preserves original modification and creation times (file timestamps are synced to match your iPhone)
- Imports only new media (photos, videos) to a local directory and keeps a database of imported files
- Cross-platform (Windows, macOS, Linux)

## Installation

### Requirements

- Python 3.9+
- iPhone connected via USB
- (If on **Windows**: [iTunes/Apple Devices app](https://support.apple.com/en-us/HT210384) must be installed and running)

We recommend you to install using [uv](https://github.com/astral-sh/uv):

```sh
uv tool install import-iphone-media
```

Alternatively, you can install using pip:

```sh
pip install import-iphone-media
```

## Usage

```
usage: import-iphone-media [-h] [--dcim-path DCIM_PATH] [--db-path DB_PATH] [--include-extensions INCLUDE_EXTENSIONS] [--verbose] output

Import media files from iPhone

positional arguments:
  output                Directory where media files should be downloaded to

options:
  -h, --help            show this help message and exit
  --dcim-path DCIM_PATH
                        Directory on iPhone to scan for media files
  --db-path DB_PATH     Path to the database file where information on imported media will be stored
  --include-extensions INCLUDE_EXTENSIONS
                        List of file extensions to include (comma-separated)
  --verbose             Enable verbose output
```

## Troubleshooting

- Ensure your iPhone is **unlocked** and you have **trusted** the computer
- If you see connection errors on Windows, make sure iTunes/Apple Devices is running
- Try reconnecting the USB cable or restarting your iPhone

## Development

Clone the repo and install in editable mode:

```sh
git clone https://github.com/artificiadrian/import-iphone-media.git
cd import-iphone-media
uv sync
```

## Acknowledgements

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)
- [rich](https://github.com/Textualize/rich)
