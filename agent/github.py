"""GitHub access via the public REST API (no gh CLI needed for the core flow).

Auth is optional: unauthenticated works for public repos (rate-limited). Set GH_TOKEN
to raise limits / enable the optional PR flow.
"""
from __future__ import annotations

import os

import requests

API = "https://api.github.com"
_TIMEOUT = 30


def _headers(token=None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "agentic-go-contributor"}
    token = token or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_issue(owner, repo, number, max_comments=5, token=None):
    """Return {title, body, labels, comments[]} for an issue."""
    h = _headers(token)
    r = requests.get(f"{API}/repos/{owner}/{repo}/issues/{number}", headers=h, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    issue = {
        "title": data.get("title", ""),
        "body": data.get("body") or "",
        "labels": [lbl.get("name", "") for lbl in data.get("labels", [])],
        "comments": [],
    }
    if max_comments:
        rc = requests.get(
            f"{API}/repos/{owner}/{repo}/issues/{number}/comments",
            headers=h, params={"per_page": max_comments}, timeout=_TIMEOUT,
        )
        if rc.ok:
            issue["comments"] = [(c.get("body") or "") for c in rc.json()[:max_comments]]
    return issue


def resolve_base_sha(owner, repo, number, token=None):
    """Best-effort: return the commit JUST BEFORE the fix (the fixing PR's merge-commit
    first parent), so the bug is present. Returns None if undeterminable (caller then
    uses the default branch)."""
    h = _headers(token)
    try:
        r = requests.get(
            f"{API}/repos/{owner}/{repo}/issues/{number}/timeline",
            headers=h, params={"per_page": 100}, timeout=_TIMEOUT,
        )
        if not r.ok:
            return None
        merge_sha = None
        for ev in r.json():
            # An issue "closed" by a commit usually records that merge/squash commit.
            if ev.get("event") == "closed" and ev.get("commit_id"):
                merge_sha = ev["commit_id"]
        if not merge_sha:
            return None
        c = requests.get(f"{API}/repos/{owner}/{repo}/commits/{merge_sha}", headers=h, timeout=_TIMEOUT)
        if c.ok:
            parents = c.json().get("parents", [])
            if parents:
                return parents[0]["sha"]
    except Exception:
        return None
    return None


def format_issue_context(issue):
    """Render the fetched issue into the text the agent receives."""
    parts = [f"Title: {issue['title']}", "", issue["body"].strip()]
    if issue.get("comments"):
        parts.append("\n--- Relevant comments ---")
        for i, c in enumerate(issue["comments"], 1):
            c = (c or "").strip()
            if c:
                parts.append(f"\n[comment {i}]\n{c[:1500]}")
    return "\n".join(parts).strip()
