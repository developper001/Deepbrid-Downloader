from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"


def release_notes_for_tag(changelog: str, tag: str) -> str:
    lines = changelog.splitlines()
    heading_pattern = re.compile(r"^## \[([^\]]+)\](?:\s+- .+)?$")
    start = None
    for index, line in enumerate(lines):
        match = heading_pattern.fullmatch(line)
        if match and match.group(1) == tag:
            start = index + 1
            break
    if start is None:
        raise ValueError(f"No changelog section found for release tag {tag}.")

    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise ValueError(f"Changelog section for release tag {tag} is empty.")
    return notes


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract release notes from CHANGELOG.md.")
    parser.add_argument("--tag", required=True, help="Release tag, for example v0.5.1")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the extracted release notes",
    )
    args = parser.parse_args()

    changelog = CHANGELOG.read_text(encoding="utf-8")
    notes = release_notes_for_tag(changelog, args.tag)
    args.output.write_text(f"{notes}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
