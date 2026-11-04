# Changelog

Notable changes are recorded here in version control so release history remains
available independently of GitHub Releases.

## [Unreleased]

- Show the same confirmation prompt for manual update checks as for startup checks.
- Add a queue context-menu action to copy filenames and show the app version in
  the startup console.
- Replace the main-window About button with Settings.

## [v0.5.1] - 2026-11-04

- Bundle cryptography's native files in release builds.
- Restore the earlier updater replacement behavior while investigating antivirus
  detections; this does not establish that the newer retry behavior caused them.

## [v0.5.0] - 2026-11-03

- Open the dedicated Chrome/Edge profile automatically when an expired queued
  Usenet link needs renewal; this behavior is configurable in Settings.
- Add a confirmed action to reset preferences and table layouts while retaining
  the API key, queued downloads, Usenet search history, and cached results.

## [v0.4.12] - 2026-11-03

- Retry transient Windows file-access failures during in-app update replacement.
- Avoid unnecessary rollback when replacement did not happen and improve
  guidance for persistent access-denied errors.
- Add updater regression tests for retries, replacement failure, and rollback.

## [v0.4.11] - 2026-11-03

- Raise minimum supported versions of filelock, platformdirs, and tkinterdnd2.
- Extend the cross-platform CI matrix to Python 3.15 and align documented
  dependency requirements with the package manifests.
- Verify dependencies with the full unit suite and pip-audit.
