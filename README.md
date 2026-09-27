# Deepbrid Downloader

A small cross-platform desktop downloader built with Python and Tkinter for Deepbrid-hosted files. It queues links, keeps progress in SQLite, resumes interrupted transfers when the server supports HTTP Range requests, and exposes a compact, easy-to-monitor interface for daily downloads.

## Features

- Queue and manage many Deepbrid-supported links from a single interface
- Keep API keys encrypted in SQLite instead of storing them in plain text
- Resume interrupted transfers using hidden `.part` files and range-aware downloads
- Show host availability and per-host quota details from Deepbrid endpoints
- Retry blocked or failed links safely, with a pause-and-resume workflow
- Toggle dark mode, show or hide the console, and configure visible columns
- Open the full project README directly from the app via the new Readme button

### Main dashboard

![Deepbrid Downloader dashboard](src/img/DeepbridDownloader.png)

### Host refresh and quota overview

![Refresh hosts](src/img/RefreshHosts.png)

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
- Right-click a row to copy the original URL, copy an available Deepbrid URL, retry, disable, force a rebuild, or remove it.
- A host refresh reads the current supported-host list and daily quota information when available.
- The app keeps active partial files and automatically resumes queued work after startup.

## Retry and resume behavior

If premium-link generation fails transiently, the app makes up to five attempts with a short pause between them, then falls back to hourly retries until it succeeds or is stopped. Cloudflare's browser-signature block is treated as non-retryable and pauses the queue. Requests use the documented Deepbrid API and the app's user agent identifier, and active downloads keep their partial files for a clean resume.

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
