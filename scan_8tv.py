#!/usr/bin/env python3
"""Find public 8TV playlist entries on GitHub, validate stream URLs, and write 8tv.txt."""

from __future__ import annotations

import base64
import html
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse

import requests

API = "https://api.github.com"
OUTPUT = Path(__file__).resolve().parent / "8tv.txt"

# GitHub code search has a maximum of 1,000 results.
PAGES_TO_SCAN = 20
RESULTS_PER_PAGE = 50
SEARCH_DELAY_SECONDS = 6.5

# Keep the search focused on playlist-like text files.
ALLOWED_SOURCE_EXTENSIONS = {".m3u", ".m3u8", ".txt"}
MAX_SOURCE_BYTES = 1_000_000

USER_AGENT = "8tv-playlist-checker/1.0"
CHANNEL_RE = re.compile(r"\b8[\s_-]*tv\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

# Do not publish URLs that appear to contain temporary or private credentials.
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "auth",
    "auth_key",
    "expires",
    "hdnea",
    "jwt",
    "key",
    "password",
    "policy",
    "sig",
    "signature",
    "token",
}


@dataclass
class Candidate:
    url: str
    source: str


@dataclass
class ValidStream:
    url: str
    latency_ms: int


def github_headers() -> dict[str, str]:
    token = os.environ.get("GH_SEARCH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET GitHub JSON, waiting if the API reports an exhausted rate limit."""
    for attempt in range(3):
        response = requests.get(
            url,
            headers=github_headers(),
            params=params,
            timeout=30,
        )

        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            reset_at = int(response.headers.get("X-RateLimit-Reset", "0"))
            wait_seconds = max(1, reset_at - int(time.time()) + 1)
            print(f"GitHub API rate limit reached; waiting {wait_seconds}s.", file=sys.stderr)
            time.sleep(wait_seconds)
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError("GitHub API rate limit remained exhausted after retries.")


def search_page(page: int) -> list[dict[str, Any]]:
    result = github_get_json(
        f"{API}/search/code",
        params={
            "q": "8tv",
            "per_page": RESULTS_PER_PAGE,
            "page": page,
        },
    )
    return result.get("items", [])


def source_text(item: dict[str, Any]) -> str | None:
    """Download a matched text file through the GitHub Contents API."""
    path = item.get("path", "")
    extension = Path(path).suffix.lower()

    if extension not in ALLOWED_SOURCE_EXTENSIONS:
        return None

    size = int(item.get("size", 0))
    if size <= 0 or size > MAX_SOURCE_BYTES:
        return None

    contents_url = item.get("url")
    if not contents_url:
        return None

    data = github_get_json(contents_url)
    if data.get("encoding") != "base64" or not data.get("content"):
        return None

    try:
        raw = base64.b64decode(data["content"])
        return raw.decode("utf-8-sig", errors="replace")
    except (ValueError, TypeError):
        return None


def is_public_candidate_url(url: str) -> bool:
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower()
    # Do not mistake GitHub source-file URLs for playable stream URLs.
    if hostname in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
        return False

    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS:
            return False

    return True


def extract_candidates(text: str, source: str) -> list[Candidate]:
    """Collect URLs close to an 8TV label in a playlist or text file."""
    lines = text.splitlines()
    found: dict[str, Candidate] = {}

    for index, line in enumerate(lines):
        if not CHANNEL_RE.search(line):
            continue

        # M3U entries normally put the stream URL on the line after #EXTINF.
        # A small surrounding window also handles simple text playlists.
        start = max(0, index - 1)
        end = min(len(lines), index + 4)

        for nearby_line in lines[start:end]:
            for match in URL_RE.findall(html.unescape(nearby_line)):
                url = match.rstrip("),.;]}")

                if is_public_candidate_url(url):
                    found.setdefault(url, Candidate(url=url, source=source))

    return list(found.values())


def find_candidates() -> list[Candidate]:
    candidates: dict[str, Candidate] = {}

    for page in range(1, PAGES_TO_SCAN + 1):
        print(f"Searching GitHub code page {page}/{PAGES_TO_SCAN}...", flush=True)

        try:
            items = search_page(page)
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else "unknown"
            raise RuntimeError(
                f"GitHub code search failed (HTTP {status}). "
                "Check the token and its code-search access."
            ) from error

        if not items:
            print("No more search results.")
            break

        for item in items:
            source = f"{item.get('repository', {}).get('full_name', '?')}/{item.get('path', '?')}"
            try:
                text = source_text(item)
            except requests.RequestException as error:
                print(f"Could not fetch {source}: {error}", file=sys.stderr)
                continue

            if not text:
                continue

            for candidate in extract_candidates(text, source):
                candidates.setdefault(candidate.url, candidate)

        # The Search API has a separate, relatively strict rate limit.
        if page < PAGES_TO_SCAN:
            time.sleep(SEARCH_DELAY_SECONDS)

    return list(candidates.values())


def looks_like_stream(response: requests.Response, body: bytes, url: str) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    path = urlparse(response.url).path.lower()
    prefix = body.lstrip()[:32].upper()

    # An HLS playlist should identify itself as M3U, not as an HTML error page.
    if prefix.startswith(b"#EXTM3U"):
        return True

    if "mpegurl" in content_type or "vnd.apple.mpegurl" in content_type:
        return True

    # For direct media files, use response MIME type rather than accepting
    # every HTTP 200 page that happens to have a media-like URL.
    media_types = (
        "video/",
        "application/octet-stream",
        "application/mp2t",
    )
    if any(media_type in content_type for media_type in media_types):
        return True

    media_suffixes = (".ts", ".flv", ".mp4", ".m4v")
    return path.endswith(media_suffixes) and not prefix.startswith(b"<!DOCTYPE HTML")


def validate_candidate(candidate: Candidate) -> ValidStream | None:
    started = time.monotonic()

    try:
        response = requests.get(
            candidate.url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Range": "bytes=0-4095",
            },
            timeout=(8, 15),
            allow_redirects=True,
            stream=True,
        )
    except requests.RequestException:
        return None

    try:
        if response.status_code not in {200, 206}:
            return None

        # Read only a small portion of the response to avoid downloading a stream.
        body = next(response.iter_content(chunk_size=4096), b"")
        if not body or not looks_like_stream(response, body, candidate.url):
            return None

        latency_ms = round((time.monotonic() - started) * 1000)
        return ValidStream(url=response.url, latency_ms=latency_ms)
    except requests.RequestException:
        return None
    finally:
        response.close()


def build_playlist(streams: list[ValidStream]) -> str:
    lines = ["#EXTM3U"]

    for index, stream in enumerate(streams, start=1):
        name = "8TV" if index == 1 else f"8TV-{index}"
        lines.append(
            f'#EXTINF:-1 tvg-id="8tv" tvg-name="{name}" '
            f'group-title="Malaysia",{name}'
        )
        lines.append(stream.url)

    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        candidates = find_candidates()
    except (requests.RequestException, RuntimeError) as error:
        print(f"Scan failed: {error}", file=sys.stderr)
        return 1

    print(f"Unique public candidates found: {len(candidates)}")
    checked: list[ValidStream] = []

    for index, candidate in enumerate(candidates, start=1):
        print(f"Checking stream {index}/{len(candidates)}...", flush=True)
        result = validate_candidate(candidate)
        if result:
            checked.append(result)

    # Keep responsive lines first; use URL as a deterministic tie-breaker.
    checked.sort(key=lambda stream: (stream.latency_ms, stream.url.lower()))

    OUTPUT.write_text(build_playlist(checked), encoding="utf-8")
    print(f"Valid streams: {len(checked)}")
    print(f"Playlist written to: {OUTPUT}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
