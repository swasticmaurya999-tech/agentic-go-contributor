"""Repository management: clone (with caching), check out the right commit, branch,
and produce the diff. Pure git — no LLM."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git(args, cwd=None, timeout=300):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _default_branch(dest):
    r = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=dest)
    ref = (r.stdout or "").strip().rsplit("/", 1)[-1]
    return ref or "main"


def prepare_repo(owner, repo, work_dir, base_sha=None, branch=None):
    """Clone (or reuse) the repo under work_dir, reset to a clean state, check out
    base_sha (or the default branch), and create a working branch. Returns the path."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / repo
    url = f"https://github.com/{owner}/{repo}.git"

    # (Re)clone if there is no valid git repo there (e.g. a previous partial/failed clone).
    if not (dest / ".git").exists():
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        res = _git(["clone", "--quiet", url, str(dest)])
        if res.returncode != 0 or not (dest / ".git").exists():
            raise RuntimeError(
                f"git clone failed for {owner}/{repo}: {(res.stderr or res.stdout or '').strip()[:200]}"
            )
    else:
        _git(["fetch", "--quiet", "origin"], cwd=dest)

    # Always start from a clean state, then check out the target commit.
    _git(["reset", "--hard", "--quiet", "HEAD"], cwd=dest)
    target = base_sha or _default_branch(dest)
    co = _git(["checkout", "--quiet", target], cwd=dest)
    if co.returncode != 0:
        raise RuntimeError(
            f"git checkout {target} failed for {owner}/{repo}: {(co.stderr or '').strip()[:200]}"
        )
    _git(["reset", "--hard", "--quiet"], cwd=dest)
    if branch:
        _git(["checkout", "-B", branch], cwd=dest)

    # Sanity check: the working tree must actually contain the source.
    if not (_git(["ls-files"], cwd=dest).stdout or "").strip():
        raise RuntimeError(
            f"{owner}/{repo} has no tracked files after checkout — clone/checkout incomplete."
        )
    return dest


def diff(dest):
    return _git(["diff"], cwd=dest).stdout or ""


def changed_files(dest):
    out = _git(["diff", "--name-only"], cwd=dest).stdout or ""
    return [line for line in out.splitlines() if line.strip()]
