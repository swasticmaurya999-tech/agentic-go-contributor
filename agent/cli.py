"""Command-line interface (Typer).

- `run`     : full pipeline from an issue URL (wired in Phase 2).
- `dev-run` : run the agent heart against an already-cloned repo + issue text
              (used to build and test the heart in Phase 1).
"""
from __future__ import annotations

from pathlib import Path

import typer

from .config import load_config
from .util import parse_issue_url

app = typer.Typer(add_completion=False, help="Agentic AI contributor for open-source Go issues.")


@app.command()
def run(
    issue: str = typer.Option(..., "--issue", help="GitHub issue URL, e.g. https://github.com/spf13/cobra/issues/1816"),
    base_sha: str = typer.Option(None, "--base-sha", help="Commit to start from (pre-fix). Auto-detected if omitted."),
    open_pr: bool = typer.Option(False, "--open-pr", help="Also push a branch and open a PR on your fork (Phase 3)."),
    allow_any: bool = typer.Option(False, "--allow-any", help="Allow repos outside the approved list."),
):
    """Full pipeline: fetch issue -> clone at the pre-fix commit -> agent fixes it ->
    emit patch.diff + PR summary."""
    from . import github, output
    from . import repo as repomod
    from .llm import LLMClient
    from .loop import run_agent
    from .runlog import RunLog
    from .tools import Tools, compute_baseline_failures
    from .util import branch_name

    log = RunLog()

    # --- config (clear message on missing key / bad provider) ---
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        log.step(f"[error]   Configuration problem: {e}")
        raise typer.Exit(2)

    try:
        owner, repo_name, number = parse_issue_url(issue)
    except ValueError as e:
        log.step(f"[error]   {e}")
        raise typer.Exit(2)
    full = f"{owner}/{repo_name}"

    if not allow_any and cfg.approved_repos and full not in cfg.approved_repos:
        log.step(f"[error]   {full} is not in the approved list. Use --allow-any to override. "
                 f"Approved: {cfg.approved_repos}")
        raise typer.Exit(2)

    # --- fetch issue ---
    try:
        log.step(f"[fetch]   {full}#{number}")
        iss = github.fetch_issue(owner, repo_name, number)
        log.step(f'[fetch]   "{iss["title"]}"')
    except Exception as e:  # noqa: BLE001
        log.step(f"[error]   Could not fetch the issue from GitHub: {str(e)[:200]}")
        log.step("[hint]    Verify it's a public issue URL and your network; set GH_TOKEN to raise API limits.")
        raise typer.Exit(1)

    # --- clone + branch at the pre-fix commit ---
    try:
        sha = base_sha or cfg.demo_base_shas.get(issue) or github.resolve_base_sha(owner, repo_name, number)
        log.step(f"[setup]   Base commit: {sha or '(default branch HEAD)'}")
        branch = branch_name(number, iss["title"])
        dest = repomod.prepare_repo(owner, repo_name, cfg.work_dir, base_sha=sha, branch=branch)
        log.step(f"[setup]   Repo ready at {dest} on branch '{branch}'")
    except Exception as e:  # noqa: BLE001
        log.step(f"[error]   Could not clone/prepare the repository: {str(e)[:200]}")
        raise typer.Exit(1)

    # --- test baseline (ignore pre-existing/OS-specific failures) ---
    log.step("[setup]   Establishing test baseline (runs the suite once)...")
    baseline = compute_baseline_failures(dest)
    log.step(f"[setup]   Baseline: {len(baseline)} pre-existing failure(s) will be ignored.")

    # --- agent ---
    issue_context = github.format_issue_context(iss)
    llm = LLMClient(cfg.llm, temperature=cfg.temperature)
    tools = Tools(dest, baseline_failures=baseline, logger=log)
    result = run_agent(llm, tools, issue_context, max_turns=cfg.max_turns, log=log)
    status = result["status"]

    # --- artifacts (only spend an LLM call on the PR summary if we truly succeeded) ---
    diff_text = repomod.diff(dest)
    files = repomod.changed_files(dest)
    use_llm = status == "done" and bool(diff_text.strip())
    title, body = output.generate_pr(
        llm if use_llm else None, issue_context, diff_text, owner, repo_name, number, iss["title"]
    )
    outdir = output.write_artifacts(cfg.out_dir, f"{repo_name}-{number}", diff_text, log.dump(), title, body)

    # --- friendly final report ---
    log.step("")
    _report_result(log, status, result["turns"], files, outdir, title)

    if open_pr and status == "done" and diff_text.strip():
        from . import pr as prmod
        prmod.open_fork_pr(dest, owner, repo_name, branch, title, body, log)
    elif open_pr:
        log.step("[pr]      Skipping PR: no validated change to submit.")

    raise typer.Exit(0 if status == "done" else 1)


def _report_result(log, status, turns, files, outdir, title):
    n = len(files)
    if status == "done":
        log.step(f"[done]    Fix complete and validated in {turns} steps ({n} file(s) changed).")
    elif status == "give_up":
        log.step(f"[give-up] The agent could not solve this issue in {turns} steps - it may be beyond "
                 "easy/medium scope or need more context.")
    elif status == "error":
        log.step(f"[error]   Run ended early (LLM/provider issue) after {turns} steps. If this is a "
                 "rate/quota limit: wait and retry, or switch LLM_MODEL/LLM_PROVIDER (e.g. a paid key).")
    else:  # max_turns
        msg = "with changes that didn't fully validate" if n else "without producing a fix"
        log.step(f"[stop]    Reached the step limit after {turns} steps {msg}.")
    if n == 0 and status not in ("error",):
        log.step("[note]    No code changes were produced - the issue may already be fixed at this commit, "
                 "need a different --base-sha, or be beyond easy/medium scope.")
    log.step(f"[output]  {outdir}   (patch.diff, pr.md, run.log)")
    log.step(f"[PR]      {title}")


@app.command("dev-run")
def dev_run(
    repo_path: str = typer.Option(..., "--repo-path", help="Path to an already-cloned Go repo (checked out at the buggy commit)."),
    issue_file: str = typer.Option(..., "--issue-file", help="Path to a text file containing the issue title + body."),
):
    """Run the agent heart against a local repo + issue text (Phase 1 testing)."""
    from .llm import LLMClient
    from .loop import run_agent
    from .runlog import RunLog
    from .tools import Tools, compute_baseline_failures

    cfg = load_config()
    log = RunLog()
    log.step(f"[setup]   Provider: {cfg.llm.provider}  model: {cfg.llm.model}")
    log.step(f"[setup]   Repository: {repo_path}")

    # Record tests that already fail on the pristine repo (e.g. OS-specific) so we can
    # ignore them when validating the agent's change.
    log.step("[setup]   Establishing test baseline (this runs the suite once)...")
    baseline = compute_baseline_failures(repo_path)
    log.step(f"[setup]   Baseline: {len(baseline)} pre-existing test failure(s) will be ignored.")

    issue_text = Path(issue_file).read_text(encoding="utf-8")
    issue_context = (
        f"GitHub issue to fix:\n\n{issue_text}\n\n"
        f"The repository is checked out and ready at the current commit. Begin."
    )

    llm = LLMClient(cfg.llm, temperature=cfg.temperature)
    tools = Tools(repo_path, baseline_failures=baseline, logger=log)
    result = run_agent(llm, tools, issue_context, max_turns=cfg.max_turns, log=log)

    status = result["status"]
    if status == "done":
        log.step(f"\n[done]    Fix complete in {result['turns']} steps.")
        log.step("[summary] " + result["final"])
    elif status == "give_up":
        log.step(f"\n[give-up] The agent could not resolve the issue (after {result['turns']} steps).")
        log.step(result["final"])
    elif status == "error":
        log.step(f"\n[error]   Run ended early due to an LLM/provider error: {result['final'][:300]}")
    else:
        log.step(f"\n[stop]    Stopped after {result['turns']} steps without a confirmed fix.")

    # Persist the full transcript (incl. verbose tool results) for inspection.
    try:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        (cfg.out_dir / "dev-run.log").write_text(log.dump(), encoding="utf-8")
        log.step(f"[log]     Full transcript: {cfg.out_dir / 'dev-run.log'}")
    except Exception:
        pass

    raise typer.Exit(code=0 if status == "done" else 1)


def main():
    app()
