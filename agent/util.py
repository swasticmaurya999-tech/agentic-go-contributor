"""Small pure helpers (URL parsing, slugs) — easy to unit-test."""
from __future__ import annotations

import re

_ISSUE_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "do", "does", "not", "when", "with",
    "for", "of", "to", "in", "on", "and", "as", "be", "that",
}


def parse_issue_url(url: str):
    """Return (owner, repo, number) from a GitHub issue URL."""
    m = _ISSUE_RE.search(url.strip())
    if not m:
        raise ValueError(
            f"Could not parse a GitHub issue URL from: {url!r}. "
            f"Expected something like https://github.com/owner/repo/issues/123"
        )
    return m.group(1), m.group(2), int(m.group(3))


def slugify(text: str, max_words: int = 4, max_len: int = 40) -> str:
    """Make a short branch-friendly slug from an issue title."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    keep = [w for w in words if w not in _STOPWORDS][:max_words]
    slug = "-".join(keep)[:max_len].strip("-")
    return slug or "fix"


def branch_name(number: int, title: str, prefix: str = "fix") -> str:
    return f"{prefix}/{number}-{slugify(title)}"
