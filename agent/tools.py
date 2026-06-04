"""The agent's tools: how the LLM inspects and modifies the cloned repo.

Each tool is a plain method operating on ``repo_dir``. ``schemas()`` exposes them
to the LLM in OpenAI function-calling format, and ``dispatch()`` runs the one the
model requested. No tool is repo-specific, so the engine works on any Go repo.

Go edits are auto-formatted with gofmt so the agent's output stays convention-clean.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_FAIL_RE = re.compile(r"--- FAIL: (\S+)")
_BUILD_ERR_MARKERS = ("[build failed]", "build failed", "cannot find package", ": syntax error")


def compute_baseline_failures(repo_dir, package="./...", timeout=600):
    """Run the suite once on the pristine repo and return the set of test names that
    already fail (e.g. OS-specific failures). Used to ignore pre-existing failures
    when validating the agent's change."""
    try:
        proc = subprocess.run(
            ["go", "test", package], cwd=str(repo_dir),
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return set()
    return set(_FAIL_RE.findall((proc.stdout or "") + (proc.stderr or "")))

# Kept deliberately small so requests stay well under tight free-tier token/min caps.
MAX_OUTPUT_CHARS = 4000
MAX_FILE_LINES = 120


def _number(lines, start):
    return "\n".join(f"{start + i}\t{line}" for i, line in enumerate(lines))


def _truncate(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


class Tools:
    def __init__(self, repo_dir, baseline_failures=None, logger=None):
        self.repo_dir = Path(repo_dir)
        self.baseline_failures = set(baseline_failures or ())
        self.log = logger
        self._dirty = True       # have the sources changed since the last run_tests?
        self._last_pass = False  # did the last run_tests pass?

    # ------------------------------------------------------------------ #
    # Tool schemas (what the LLM sees)
    # ------------------------------------------------------------------ #
    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": (
                        "Search the repository's tracked files (uses git grep). "
                        "Returns 'file:line: text' matches. Use this FIRST to locate "
                        "function names, identifiers, or error strings from the issue."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Text/regex to search for (e.g. isFlagArg)."},
                            "max_results": {"type": "integer", "description": "Max lines to return (default 60)."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "Read a file. Provide start_line and end_line to read only a range "
                        "(recommended for large files). Returns lines prefixed with line numbers."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Repo-relative file path."},
                            "start_line": {"type": "integer", "description": "First line (1-based)."},
                            "end_line": {"type": "integer", "description": "Last line (inclusive)."},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List tracked files under a directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "directory": {"type": "string", "description": "Repo-relative dir (default repo root)."},
                            "max_results": {"type": "integer"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": (
                        "Replace an exact, unique snippet old_string with new_string in a file. "
                        "old_string must appear EXACTLY ONCE. Keep edits minimal. .go files are "
                        "auto-formatted with gofmt after the edit."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_string": {"type": "string", "description": "Exact text to replace (unique in the file)."},
                            "new_string": {"type": "string", "description": "Replacement text."},
                        },
                        "required": ["path", "old_string", "new_string"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "replace_lines",
                    "description": (
                        "Replace an inclusive range of lines [start_line, end_line] in a file with "
                        "new_string (may span multiple lines). Use the line numbers shown by read_file. "
                        "Most reliable way to edit. .go files are auto-formatted with gofmt afterwards, "
                        "so don't worry about exact indentation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "description": "First line to replace (1-based, inclusive)."},
                            "end_line": {"type": "integer", "description": "Last line to replace (inclusive)."},
                            "new_string": {"type": "string", "description": "Replacement text for those lines."},
                        },
                        "required": ["path", "start_line", "end_line", "new_string"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_tests",
                    "description": (
                        "Run Go tests. Strongly prefer a focused run: set 'run' to a regex matching the "
                        "test(s) relevant to your change, and 'package' to the specific package. "
                        "The full './...' suite may contain unrelated, environment-specific failures."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "package": {"type": "string", "description": "Go package pattern (default '.', the repo-root package)."},
                            "run": {"type": "string", "description": "Regex for -run, e.g. 'TestFlagCompletion'. Highly recommended."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_build",
                    "description": "Run 'go build ./...' to check the code compiles.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
        ]

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def dispatch(self, name, args):
        fn = getattr(self, f"_tool_{name}", None)
        if fn is None and "." in name:
            name = name.split(".")[-1]  # strip hallucinated namespace, e.g. repo_browser.list_files
            fn = getattr(self, f"_tool_{name}", None)
        if fn is None:
            return (
                f"ERROR: unknown tool '{name}'. Available: search_code, read_file, list_files, "
                "edit_file, replace_lines, run_tests, run_build."
            )
        args = dict(args or {})
        # Tolerate common alias param names the model sometimes invents.
        _aliases = {
            "line_start": "start_line", "line_end": "end_line", "start": "start_line",
            "end": "end_line", "file": "path", "file_path": "path", "filename": "path",
        }
        for alias, real in _aliases.items():
            if alias in args and real not in args:
                args[real] = args.pop(alias)
        try:
            return fn(**args)
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 - surface any tool error to the model
            return f"ERROR running {name}: {e}"

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _run(self, cmd, timeout=300):
        proc = subprocess.run(
            cmd, cwd=self.repo_dir, capture_output=True, text=True, timeout=timeout
        )
        return (proc.stdout or "") + (proc.stderr or "")

    def _safe(self, path):
        """Resolve a repo-relative path and ensure it stays inside the repo (scope guard)."""
        try:
            fp = (self.repo_dir / path).resolve()
        except Exception:
            return None
        root = self.repo_dir.resolve()
        return fp if (fp == root or root in fp.parents) else None

    def _gofmt(self, path):
        """Run gofmt -w on a .go file. Returns gofmt's stderr (i.e. a syntax error if the
        edit produced invalid Go), or '' on success / non-Go files."""
        if not str(path).endswith(".go"):
            return ""
        try:
            proc = subprocess.run(
                ["gofmt", "-w", str(path)],
                cwd=self.repo_dir, capture_output=True, text=True, timeout=30,
            )
            return (proc.stderr or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _tool_search_code(self, query, max_results=60):
        out = self._run(["git", "grep", "-n", "-I", "-e", query], timeout=60)
        if not out.strip():
            return f"No matches for: {query}"
        return "\n".join(out.splitlines()[: int(max_results)])

    def _tool_read_file(self, path, start_line=None, end_line=None):
        fp = self._safe(path)
        if fp is None:
            return f"ERROR: path is outside the repository: {path}"
        if not fp.exists():
            return f"ERROR: file not found: {path}"
        text = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        n = len(text)
        if start_line is None and end_line is None:
            if n > MAX_FILE_LINES:
                return (
                    f"{path} has {n} lines; showing 1-{MAX_FILE_LINES}. "
                    f"Use start_line/end_line to read a specific range.\n"
                    + _number(text[:MAX_FILE_LINES], 1)
                )
            return _number(text, 1)
        s = max(1, int(start_line or 1))
        e = min(n, int(end_line or n))
        if e - s + 1 > MAX_FILE_LINES:  # keep context small even for explicit ranges
            e = s + MAX_FILE_LINES - 1
        return _number(text[s - 1 : e], s)

    def _tool_list_files(self, directory=".", max_results=300):
        out = self._run(["git", "ls-files", directory], timeout=60)
        files = out.splitlines()[: int(max_results)]
        return "\n".join(files) if files else "(no tracked files)"

    def _tool_edit_file(self, path, old_string, new_string):
        fp = self._safe(path)
        if fp is None:
            return f"ERROR: path is outside the repository: {path}"
        if not fp.exists():
            return f"ERROR: file not found: {path}"
        text = fp.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            return (
                "ERROR: old_string not found (exact match failed — often a tabs/whitespace "
                "mismatch). Do NOT retry the same edit. Instead use replace_lines with the line "
                "numbers from read_file."
            )
        if count > 1:
            return f"ERROR: old_string matches {count} places; include more context, or use replace_lines."
        fp.write_text(text.replace(old_string, new_string), encoding="utf-8")
        self._dirty = True
        gofmt_err = self._gofmt(path)
        if gofmt_err:
            return (
                f"Edit applied to {path}, BUT it produced INVALID Go - your edit broke the code "
                f"structure (likely unbalanced braces). Fix it now:\n{gofmt_err[:400]}"
            )
        return f"Edit applied to {path} (gofmt clean)."

    def _tool_replace_lines(self, path, start_line, end_line, new_string):
        fp = self._safe(path)
        if fp is None:
            return f"ERROR: path is outside the repository: {path}"
        if not fp.exists():
            return f"ERROR: file not found: {path}"
        content = fp.read_text(encoding="utf-8")
        lines = content.split("\n")
        n = len(lines)
        s, e = int(start_line), int(end_line)
        if s < 1 or e > n or s > e:
            return f"ERROR: invalid range {s}-{e}; file has {n} lines."
        new_lines = new_string.split("\n")
        result = lines[: s - 1] + new_lines + lines[e:]
        fp.write_text("\n".join(result), encoding="utf-8")
        self._dirty = True
        gofmt_err = self._gofmt(path)
        # Re-read (gofmt may have adjusted formatting) to show accurate context.
        after = fp.read_text(encoding="utf-8", errors="replace").split("\n")
        ctx_start = max(1, s - 2)
        ctx_end = min(len(after), s - 1 + len(new_lines) + 2)
        snippet = _number(after[ctx_start - 1 : ctx_end], ctx_start)
        if gofmt_err:
            return (
                f"Replaced lines {s}-{e} in {path}, BUT it produced INVALID Go - your range likely "
                f"removed adjacent code or unbalanced braces. Fix it now:\n{gofmt_err[:400]}\n\n"
                f"Current context:\n{snippet}"
            )
        return f"Replaced lines {s}-{e} in {path} (gofmt clean). New surrounding context:\n{snippet}"

    def _tool_run_tests(self, package="./...", run=None):
        cmd = ["go", "test"]
        if run:
            cmd += ["-run", run]
        cmd.append(package or "./...")
        proc = subprocess.run(
            cmd, cwd=self.repo_dir, capture_output=True, text=True, timeout=600
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        self._dirty = False  # we have just tested the current state of the sources

        # Compile/build errors are always real failures.
        if any(m in out for m in _BUILD_ERR_MARKERS):
            self._last_pass = False
            return "BUILD/COMPILE ERROR (fix the code so it compiles):\n" + _truncate(out)

        _finish = " If your fix is complete, reply 'DONE:' now (a regression test is encouraged but optional)."
        if proc.returncode == 0:
            self._last_pass = True
            return "ALL TESTS PASS." + _finish

        current = set(_FAIL_RE.findall(out))
        new_failures = current - self.baseline_failures
        if not new_failures:
            self._last_pass = True
            ignored = sorted(current & self.baseline_failures)
            return (
                "RELEVANT TESTS PASS. "
                f"Ignored {len(ignored)} pre-existing (environment-specific) failure(s) "
                f"that also fail on the unmodified repo: {ignored}." + _finish
            )
        self._last_pass = False
        return (
            "TESTS FAILED. These tests fail because of your change (not pre-existing): "
            f"{sorted(new_failures)}\n\n" + _truncate(out)
        )

    def _tool_run_build(self):
        out = self._run(["go", "build", "./..."], timeout=600)
        return _truncate(out) or "Build succeeded (no output)."

    # ------------------------------------------------------------------ #
    # Completion gate (used by the loop to verify a claimed "DONE")
    # ------------------------------------------------------------------ #
    def has_changes(self):
        return bool(self._run(["git", "diff", "--stat"], timeout=60).strip())

    def verify(self):
        """Return (ok, reason). Completion is accepted only if real changes exist AND the
        relevant tests pass. Reuses the last run_tests result when nothing has changed
        since (avoids a redundant full test run after a passing run_tests)."""
        if not self.has_changes():
            return False, "No code changes were made. You must actually edit the code to fix the issue."
        if not self._dirty and self._last_pass:
            return True, "Changes present and the relevant tests already passed (no edits since)."
        result = self._tool_run_tests()
        if "ALL TESTS PASS" in result or "RELEVANT TESTS PASS" in result:
            return True, "Changes are present and the relevant tests pass."
        return False, "Tests are not passing yet:\n" + result
