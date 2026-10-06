from __future__ import annotations

import argparse
import sys
from pathlib import Path

from platformdirs import user_data_dir

from src.usenet_browser import UsenetBrowserSession
from src.usenet_finder import UsenetFinderClient, UsenetFinderError


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Deepbrid's undocumented Finder endpoint in the dedicated Chrome profile."
        )
    )
    parser.add_argument("query", help="Search query")
    parser.add_argument("--category", default="", help="Optional Finder category identifier")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument(
        "--resolve-first",
        action="store_true",
        help="Also resolve the first result and report its file schema",
    )
    args = parser.parse_args()

    profile = Path(user_data_dir("Deepbrid Downloader", "Deepbrid")) / "usenet-browser-profile"
    browser = UsenetBrowserSession(profile, log=print)
    client = UsenetFinderClient(browser)
    try:
        browser.open()
        page = client.search(args.query, args.category, args.offset, args.limit)
        print(f"Search succeeded. Results returned: {len(page.results)}; hasMore: {page.has_more}")
        if page.results:
            result = page.results[0]
            print(f"First result fields: {', '.join(sorted(result.metadata))}")
            print(f"First result title: {result.title}")
            print(f"First result category: {result.category or '(not supplied)'}")
            print(f"First result sizeBytes: {result.size_bytes}")
            if args.resolve_first:
                package = client.resolve(result.token)
                file_fields = sorted(package.files[0].metadata) if package.files else []
                accessible_count = sum(file.is_accessible for file in package.files)
                print(
                    f"Resolve succeeded. Files: {len(package.files)}; "
                    f"accessible: {accessible_count}; pkg: {package.name or '(not supplied)'}"
                )
                print(f"First file fields: {', '.join(file_fields) or '(no files)'}")
        elif args.resolve_first:
            print("No result to resolve.")
    except (UsenetFinderError, ValueError) as error:
        print(f"Finder probe failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
