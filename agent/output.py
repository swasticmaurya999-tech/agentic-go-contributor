"""Turn the agent's work into reviewable artifacts: patch.diff, run.log, pr.md."""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from .prompts import PR_SUMMARY_PROMPT


_ASCII = {
    "‑": "-", "–": "-", "—": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
}


def _ascii(s):
    out = []
    for ch in s:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in _ASCII:
            out.append(_ASCII[ch])
        elif unicodedata.category(ch) == "Zs":  # any unicode space -> plain space
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def write_artifacts(out_dir, slug, diff_text, run_log_text, pr_title, pr_body):
    d = Path(out_dir) / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "patch.diff").write_text(diff_text or "", encoding="utf-8")
    (d / "run.log").write_text(run_log_text or "", encoding="utf-8")
    (d / "pr.md").write_text(f"# {_ascii(pr_title)}\n\n{_ascii(pr_body)}\n", encoding="utf-8")
    return d


def _fallback_pr(owner, repo, number, title):
    return (
        f"fix: {title}".strip()[:72],
        f"## Summary\nAddresses {owner}/{repo}#{number}.\n\n## Testing\n`go test` passes "
        f"for the affected package.\n\nRefs {owner}/{repo}#{number}",
    )


def generate_pr(llm, issue_context, diff_text, owner, repo, number, issue_title=""):
    """Ask the LLM for a PR title + body from the issue and the diff. Falls back to a
    template if the LLM is unavailable or returns something unparseable."""
    if llm is None or not diff_text.strip():
        return _fallback_pr(owner, repo, number, issue_title)
    messages = [
        {"role": "system", "content": PR_SUMMARY_PROMPT},
        {
            "role": "user",
            "content": (
                f"Repository: {owner}/{repo}  Issue #{number}\n\n"
                f"Issue:\n{issue_context[:3000]}\n\nFinal diff:\n{diff_text[:6000]}"
            ),
        },
    ]
    try:
        resp = llm.complete(messages)  # no tools -> plain completion
        text = (resp.content or "").strip()
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        title = (data.get("title") or "").strip()
        body = (data.get("body") or "").strip()
        if title and body:
            return title[:72], body
    except Exception:
        pass
    return _fallback_pr(owner, repo, number, issue_title)
