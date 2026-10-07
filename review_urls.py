"""Shared parsing of queue references and canonical PR/MR identities."""

import re
from urllib.parse import urlsplit


GITLAB_MR_PATH_PATTERN = re.compile(r"(?P<project>/(?:[^/]+/)*[^/]+)/-/merge_requests/(?P<number>[0-9]+)")
GITHUB_PR_PATH_PATTERN = re.compile(r"(?P<project>/[^/]+/[^/]+)/pull/(?P<number>[0-9]+)")


def normalize_review_number(number: str) -> str:
    return number.lstrip("0") or "0"


def parse_review_url(url: str) -> tuple[str, tuple[str, str, str]] | None:
    """Return the display reference and URL identity for a PR/MR link."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not hostname or parsed.username is not None:
        return None

    path = parsed.path.rstrip("/")
    if hostname.lower() == "github.com":
        match = GITHUB_PR_PATH_PATTERN.fullmatch(path)
    else:
        match = GITLAB_MR_PATH_PATTERN.fullmatch(path)
    if match is None:
        return None

    project = match.group("project")
    number = match.group("number")
    repo = project.rsplit("/", 1)[-1]
    return f"{repo}/{number}", (parsed.netloc.lower(), project, normalize_review_number(number))
