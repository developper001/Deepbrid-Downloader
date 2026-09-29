from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools.config.pyprojecttoml import read_configuration

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ARTIFACTS = ROOT / "release-artifacts"
VERSION = read_configuration(str(ROOT / "pyproject.toml"))["project"]["version"]


def sha256sum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=ROOT)


def publish_binary(binary_path: Path, platform: str, extension: str = "") -> list[Path]:
    stable_path = ARTIFACTS / f"DeepbridDownloader-{platform}{extension}"
    legacy_path = ARTIFACTS / f"DeepbridDownloader-{platform}-{VERSION}{extension}"
    for output_path in (stable_path, legacy_path):
        output_path.unlink(missing_ok=True)
        shutil.copy2(binary_path, output_path)
    return [stable_path, legacy_path]


def build_windows() -> list[Path]:
    out_dir = DIST / "windows"
    run([
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--noconsole",
        "--icon",
        str(ROOT / "src" / "deepbrid-favicon.ico"),
        "--name",
        "DeepbridDownloader",
        "--distpath",
        str(out_dir),
        "launcher.py",
    ])
    exe_path = out_dir / "DeepbridDownloader.exe"
    if not exe_path.exists():
        raise FileNotFoundError(f"Expected built exe at {exe_path}")
    return publish_binary(exe_path, "Windows", ".exe")


def build_linux() -> list[Path]:
    out_dir = DIST / "linux"
    run([
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "DeepbridDownloader",
        "--distpath",
        str(out_dir),
        "launcher.py",
    ])
    bin_path = out_dir / "DeepbridDownloader"
    if not bin_path.exists():
        raise FileNotFoundError(f"Expected built binary at {bin_path}")
    return publish_binary(bin_path, "Linux")


def build_macos() -> list[Path]:
    out_dir = DIST / "macos"
    run([
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "DeepbridDownloader",
        "--distpath",
        str(out_dir),
        "launcher.py",
    ])
    bin_path = out_dir / "DeepbridDownloader"
    if not bin_path.exists():
        raise FileNotFoundError(f"Expected built binary at {bin_path}")
    return publish_binary(bin_path, "macOS")


def write_checksums(files: list[Path]) -> Path:
    checksums_path = ARTIFACTS / "checksums.txt"
    lines = []
    for file in files:
        if file.exists():
            lines.append(f"{sha256sum(file)}  {file.name}")
    checksums_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return checksums_path


def main() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    built = [*build_windows(), *build_linux(), *build_macos()]
    checksums = write_checksums(built)
    print(f"Built: {', '.join(str(path) for path in built)}")
    print(f"Checksums: {checksums}")


if __name__ == "__main__":
    main()
