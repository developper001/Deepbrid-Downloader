# Deepbrid Downloader

A small cross-platform desktop downloader built with Python and Tkinter for Deepbrid-hosted files. It queues links, keeps progress in SQLite, resumes interrupted transfers when the server supports HTTP Range requests, and exposes a compact, easy-to-monitor interface for daily downloads.

## Supported platforms

- Windows 10/11
- macOS 12+
- Linux (Ubuntu and other mainstream distributions with Tkinter available)

## Requirements

- Python 3.10+
- Tkinter support for your OS
- `cryptography>=42`
- `platformdirs>=4`

## Installation

```sh
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Then run:

```sh
python -m src.app
```

Or use the launcher:

```sh
python launcher.py
```

## Features

- Queue and manage many Deepbrid-supported links from a single interface
- Keep API keys encrypted in SQLite instead of storing them in plain text
- Resume interrupted transfers using hidden `.part` files and range-aware downloads
- Open a searchable, sortable Host Status window with live availability and daily quota details
- Show the cached host list immediately, then refresh it in the background; open the API-key page when Deepbrid returns HTTP 401
- Select multiple queue rows with Ctrl-click or a Shift-click range, then apply context-menu actions in display order
- Retry blocked or failed links safely, with a pause-and-resume workflow
- Toggle dark mode, show or hide the console, and configure visible columns
- Open the Deepbrid dashboard directly from missing or invalid API-key prompts
- Open the GitHub repository and check for newer releases from the About window
- Use the user's OS Downloads folder by default when available, retaining a saved folder choice
- Route standard output, errors, and uncaught exceptions to the in-app console
- Build the Windows executable with the Deepbrid icon and no separate console window
- Open the full project README directly from the About window

### Main dashboard

![Deepbrid Downloader dashboard](src/img/DeepbridDownloader.png)

### Host status and quota overview

![Refresh hosts](src/img/RefreshHosts.png)

### About and release updates

![About window with GitHub link and update check](src/img/AboutAndUpdates.png)

### Column visibility and queue management

![Columns and queue options](src/img/OrderColumns.png)

### Right-click actions and retry flow

![Right-click menu](src/img/RightClickMenu.png)

![Retry workflow](src/img/RetrySystem.png)

![Safe retry handling](src/img/SafeRetry.png)

### Dark mode and console logging

![Dark mode](src/img/DarkMode.png)

![Console logs](src/img/ConsoleFullLogs.png)

## Run

Use Python 3.10 or newer with Tkinter available. Install the dependencies and launch the app from the project root:

```sh
python -m pip install -r requirements.txt
python -m src.app
```

On Linux, Tkinter may need to be installed through the operating system package manager. The API key, original URLs, generated Deepbrid URLs, and the random AES-GCM encryption key are stored in `src/deepbrid_downloader.sqlite3`, so the database works across operating systems without a system keychain. Existing `.env` keys are migrated and the file removed only after the encrypted value is verified.

## How it works

- Paste one or more supported links or HTML containing links into the input box.
- The app filters for hosts supported by Deepbrid and ignores unsupported ones.
- Queue priority follows the displayed row order and updates when links are added, removed, or sorted.
- Rows show status, downloaded/total bytes, remaining bytes, and ETA.
- Click a row to select it, Ctrl-click to add or toggle rows, or Shift-click to select a range; right-click a selected row to apply context-menu actions to the selection in display order.
- The Host Status window opens with the last cached host list, then refreshes availability and daily quotas in the background. Search by host or sort by any column.
- The app keeps active partial files and automatically resumes queued work after startup.
- If the API key is missing or rejected, follow the Deepbrid link in the prompt or Host Status window to retrieve or replace it.
- Use the About window to open the GitHub repository and check whether a newer release is available.

## Retry and resume behavior

If premium-link generation fails transiently, the app makes up to five attempts with a short pause between them, then falls back to hourly retries until it succeeds or is stopped. Cloudflare's browser-signature block is treated as non-retryable and pauses the queue. Requests use the documented Deepbrid API and the app's user agent identifier, and active downloads keep their partial files for a clean resume.

## Tests

The project uses the standard library unittest suite. Run it locally with:

```sh
python -m unittest -q tests.test_downloader
```

GitHub Actions runs the same tests automatically on Ubuntu, Windows, and macOS for supported Python versions.
It also runs `pip-audit` against `requirements.txt` on pushes and pull requests, with a weekly scheduled audit. The weekly run skips the platform test matrix.

## GitHub CI

This repository includes a minimal GitHub Actions workflow at `.github/workflows/ci.yml`.

It runs:
- Python 3.10
- Python 3.11
- Python 3.12
- on Windows, macOS, and Ubuntu
- `python -m unittest -q tests.test_downloader`
- a package build step with `python -m build`

## Minimal installer for Windows, macOS, and Linux

For a public repo, the minimal production path is to keep the source public and ship platform build artifacts as a GitHub Release.

Recommended minimal pipeline:
- publish the source repo publicly
- tag a release like `v0.2.7`
- let GitHub Actions build the app for Windows, macOS, and Linux
- attach the binaries and the generated checksum file to the release

Build and release steps:

```sh
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install build pyinstaller
python -m build
python -m PyInstaller --onefile --windowed launcher.py
```

Create the git tag and push it:

```sh
git tag v0.2.7
git push origin v0.2.7
```

This triggers the GitHub release workflow in `.github/workflows/release.yml`, which produces versioned platform binaries (for example, `DeepbridDownloader-Windows-0.2.7.exe`) and a `sha256sums.txt` file.
The Windows build embeds the Deepbrid icon and runs without opening a separate terminal console; output and uncaught exceptions are routed to the app's in-app console.

Notes:
- `pyinstaller` is the simplest way to produce a single-file app for Windows/macOS/Linux.
- For a more polished distribution, a native installer such as an MSI or DMG is better than a bare executable.
- The repo remains the source of truth; the GitHub release is the production distribution.

## Signature

This project was coded with Copilot.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for the full license text.

MIT License

Copyright (c) 2026 Deepbrid Downloader contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
