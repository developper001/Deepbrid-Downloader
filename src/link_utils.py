from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse


URL_PATTERN = re.compile(r"https?://[^\s<>\"'()]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?]}"


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for attribute, value in attrs:
            if attribute in {"href", "src", "data-url"} and value:
                self.links.append(value)


def _host_for(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


def supported_link_status(url: str, hosts: dict[str, str]) -> tuple[str, str] | None:
    hostname = _host_for(url)
    matches = []
    for domain, status in hosts.items():
        normalized_domain = domain.lower().removeprefix("www.").rstrip(".")
        domain_matches = hostname == normalized_domain or hostname.endswith(f".{normalized_domain}")
        if "." not in normalized_domain:
            domain_matches = domain_matches or normalized_domain in hostname.split(".")
        if domain_matches:
            matches.append((len(normalized_domain), normalized_domain, status.strip()))
    if not matches:
        return None
    _, domain, status = max(matches)
    normalized_status = status.split("(", 1)[0].strip().lower()
    message = f"{normalized_status.upper()}: {domain}" if normalized_status else f"UNKNOWN: {domain}"
    return normalized_status, message


def extract_http_links(text: str) -> list[str]:
    parser = _LinkParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        parser.links.clear()

    candidates = [*parser.links, *URL_PATTERN.findall(text)]
    results: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        link = html.unescape(candidate.strip()).rstrip(TRAILING_PUNCTUATION)
        parsed = urlparse(link)
        if (
            link in seen
            or parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
        ):
            continue
        seen.add(link)
        results.append(link)
    return results


def extract_supported_links(text: str, hosts: dict[str, str]) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for link in extract_http_links(text):
        status = supported_link_status(link, hosts)
        if status is not None:
            results.append((link, status[0], status[1]))
    return results