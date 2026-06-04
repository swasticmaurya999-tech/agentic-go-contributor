"""Optional PR opening on the USER'S FORK (never upstream — avoids spamming maintainers).

Requires the gh CLI. Degrades gracefully (prints guidance, returns None) if gh is
missing or unauthenticated, so the default patch/pr.md flow is never affected.
"""
from __future__ import annotations

import shutil
import subprocess


def _run(args, cwd=None, timeout=180):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def gh_available():
    return shutil.which("gh") is not None


def _gh_user():
    r = _run(["gh", "api", "user", "--jq", ".login"])
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def open_fork_pr(dest, owner, repo, branch, title, body, log):
    """Commit the agent's changes, push the branch to the user's fork, and open a
    self-PR on that fork. Returns the PR URL or None. Never touches upstream."""
    if not gh_available():
        log.step("[pr]      gh CLI not found — install it (winget install GitHub.cli) or use Docker. "
                 "Skipping PR (patch.diff + pr.md are still produced).")
        return None
    user = _gh_user()
    if not user:
        log.step("[pr]      Not authenticated. Run 'gh auth login' or set GH_TOKEN. Skipping PR.")
        return None

    # Commit the working-tree changes on the branch (self-contained bot identity).
    _run(["git", "-C", str(dest), "add", "-A"])
    commit = _run([
        "git", "-C", str(dest),
        "-c", "user.email=agent@local", "-c", "user.name=agentic-go-contributor",
        "commit", "-m", title,
    ])
    if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr).lower():
        log.step("[pr]      No changes to commit — skipping PR.")
        return None

    fork = f"{user}/{repo}"
    log.step(f"[pr]      Ensuring fork {fork} exists...")
    _run(["gh", "repo", "fork", f"{owner}/{repo}", "--clone=false"])  # idempotent

    _run(["git", "-C", str(dest), "remote", "remove", "fork"])
    _run(["git", "-C", str(dest), "remote", "add", "fork", f"https://github.com/{fork}.git"])
    push = _run(["git", "-C", str(dest), "push", "-u", "fork", branch, "--force"])
    if push.returncode != 0:
        log.step(f"[pr]      Push to fork failed: {(push.stderr or '').strip()[:200]}")
        return None

    base = _run(["gh", "repo", "view", fork, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"])
    base_branch = (base.stdout or "").strip() or "main"

    pr = _run([
        "gh", "pr", "create", "--repo", fork,
        "--base", base_branch, "--head", f"{user}:{branch}",
        "--title", title, "--body", body,
    ])
    out = (pr.stdout or "").strip()
    if pr.returncode == 0 and out:
        url = out.splitlines()[-1]
        log.step(f"[pr]      Opened PR on your fork: {url}")
        return url
    log.step(f"[pr]      PR creation message: {(out or pr.stderr or '').strip()[:200]}")
    return None
