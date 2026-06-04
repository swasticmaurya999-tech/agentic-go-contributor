"""The agent loop: the heart of the system.

Repeatedly: ask the LLM what to do -> if it requests tools, run them and feed the
results back -> stop when the model produces a final (non-tool) message. The loop
itself is intentionally dumb; all intelligence lives in the model + tools + prompt.

It also narrates progress in human-readable terms (analysing, locating, fixing,
testing) so a user watching the console understands what is happening.
"""
from __future__ import annotations

import json

from .prompts import SYSTEM_PROMPT


def _prune_history(messages, max_chars=14000, keep_recent_tools=2):
    """Bound the conversation size so each request stays under tight free-tier
    token/minute caps. Older tool results are truncated (the model can re-read if
    needed); the system prompt, the issue, and the most recent tool results stay full.
    """
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protect = set(tool_idxs[-keep_recent_tools:])
    total = sum(len(str(m.get("content", ""))) for m in messages)
    for i in tool_idxs:
        if total <= max_chars:
            break
        if i in protect:
            continue
        c = str(messages[i].get("content", ""))
        if len(c) > 200:
            new_c = c[:160] + " ...[older tool output truncated to save context]"
            messages[i]["content"] = new_c
            total -= len(c) - len(new_c)


def _describe(name, args):
    """Turn a raw tool call into a friendly progress line (ASCII-safe)."""
    if name == "search_code":
        return f"[search]  Searching the codebase for: {args.get('query', '')}"
    if name == "read_file":
        rng = ""
        if args.get("start_line"):
            rng = f" (lines {args.get('start_line')}-{args.get('end_line', '')})"
        return f"[read]    Reading {args.get('path', '')}{rng}"
    if name == "list_files":
        return f"[list]    Exploring files under {args.get('directory', '.')}"
    if name in ("edit_file", "replace_lines"):
        return f"[edit]    Applying a fix to {args.get('path', '')}"
    if name == "run_tests":
        return f"[test]    Running tests ({args.get('package', './...')})"
    if name == "run_build":
        return "[build]   Compiling the project"
    return f"[tool]    {name}"


def _test_outcome(result):
    """Best-effort read of a go-test result into a one-line verdict."""
    text = str(result)
    if "TESTS PASS" in text:
        return "          -> tests passed"
    if "TESTS FAILED" in text or "BUILD/COMPILE ERROR" in text:
        return "          -> tests failed; analysing the failure to correct it..."
    return None


def run_agent(llm, tools, issue_context, max_turns=25, log=None):
    step = log.step if log is not None else print
    detail = log.detail if log is not None else (lambda *_: None)

    step("[plan]    Analysing the issue and planning a fix...")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": issue_context},
    ]
    schemas = tools.schemas()
    seen = {}  # de-dupe repeated read-only calls so the agent doesn't waste steps
    read_counts = {}  # cap repeated reads of the same file (avoids test-phase wandering)
    false_done = 0  # times the model claimed completion without a verified fix

    for turn in range(1, max_turns + 1):
        _prune_history(messages)
        try:
            resp = llm.complete(messages, tools=schemas)
        except Exception as e:  # noqa: BLE001 - never crash; end the run gracefully
            # The model failed on this step — but if the fix is already validated, the
            # task is done regardless of the model's final-message reliability.
            ok, _ = tools.verify()
            if ok:
                step("[verify]  Fix is validated (changes + tests pass) — finishing despite a model error.")
                return {"status": "done", "final": "DONE — fix validated; model errored on a later step.",
                        "turns": turn, "messages": messages}
            step(f"[error]   LLM call failed after retries: {str(e)[:200]}")
            return {"status": "error", "final": str(e), "turns": turn, "messages": messages}

        # Record the assistant turn (preserving tool_calls for the next request).
        assistant = {"role": "assistant", "content": resp.content or ""}
        if resp.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in resp.tool_calls
            ]
        messages.append(assistant)

        # No tool calls => the model claims it is done (or gave up).
        if not resp.tool_calls:
            text = (resp.content or "").strip()
            if text.upper().startswith("GIVE_UP"):
                detail(f"[final turn {turn}] {text}")
                return {"status": "give_up", "final": text, "turns": turn, "messages": messages}

            # Don't trust the claim — verify real changes exist AND tests pass.
            ok, reason = tools.verify()
            if ok:
                step("[verify]  Changes present and relevant tests pass.")
                return {"status": "done", "final": text or "DONE", "turns": turn, "messages": messages}

            false_done += 1
            step(f"[verify]  Not finished: {reason.splitlines()[0]}")
            if false_done >= 3:
                return {"status": "give_up", "final": "Claimed completion without a verified fix. " + reason,
                        "turns": turn, "messages": messages}
            messages.append({
                "role": "user",
                "content": (
                    "You are NOT done. " + reason + "\nContinue working with the tools: locate the "
                    "code, make the fix, add a regression test, and run_tests. Only stop once tests pass."
                ),
            })
            continue

        # Execute each requested tool and feed results back.
        for tc in resp.tool_calls:
            name = tc.name
            try:
                args = json.loads(tc.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            step(_describe(name, args))
            detail(f"[turn {turn}] {name}({json.dumps(args)[:400]})")

            key = name + "|" + json.dumps(args, sort_keys=True)
            path = args.get("path", "")
            exact_repeat = name in ("search_code", "read_file", "list_files") and seen.get(key)
            over_read = name == "read_file" and read_counts.get(path, 0) >= 3
            if over_read:
                result = (
                    f"(You have already read {path} several times. STOP reading it. Decide NOW: add "
                    "your regression test with replace_lines, or — if run_tests already passed — reply "
                    "with 'DONE:' to finish.)"
                )
            elif exact_repeat:
                result = (
                    "(Skipped: you already ran this exact call. Do not repeat searches or reads. "
                    "Edit the code, add a test, run tests, or reply DONE.)"
                )
            else:
                result = tools.dispatch(name, args)
            seen[key] = seen.get(key, 0) + 1
            if name == "read_file":
                read_counts[path] = read_counts.get(path, 0) + 1
            detail(f"[turn {turn}] -> {str(result)[:800]}")

            if name in ("run_tests", "run_build"):
                verdict = _test_outcome(result)
                if verdict:
                    step(verdict)

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

    # Step limit reached — still a success if the fix is actually validated.
    ok, _ = tools.verify()
    if ok:
        step("[verify]  Step limit reached, but the fix is validated (changes + tests pass).")
        return {"status": "done", "final": "DONE — fix validated at the step limit.",
                "turns": max_turns, "messages": messages}
    step("[stop]    Reached the step limit without a confirmed fix.")
    return {"status": "max_turns", "final": "", "turns": max_turns, "messages": messages}
