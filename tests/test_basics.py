"""Unit tests for our own logic (no network / no LLM / no Go).

Validates the harness BEFORE we ever run the agent. Run from the project root:
    python tests\test_basics.py      (or: python -m pytest tests)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import PROVIDERS, load_config  # noqa: E402
from agent.github import format_issue_context  # noqa: E402
from agent.llm import (  # noqa: E402
    LLMResponse,
    ToolCall,
    _is_rate_limit,
    _is_toolcall_error,
    _parse_content_tool_calls,
    _parse_retry_after,
    _repair_json,
    _salvage_tool_calls,
    friendly_error,
)
from agent.loop import _prune_history, run_agent  # noqa: E402
from agent.output import _ascii  # noqa: E402
from agent.tools import _FAIL_RE, MAX_FILE_LINES, Tools  # noqa: E402
from agent.util import branch_name, parse_issue_url, slugify  # noqa: E402


class FakeErr(Exception):
    """Stand-in for an openai APIStatusError (has .status_code and .body)."""

    def __init__(self, msg, status_code=None, body=None):
        super().__init__(msg)
        self.status_code = status_code
        self.body = body or {}


# --------------------------- util ---------------------------
def test_parse_issue_url():
    assert parse_issue_url("https://github.com/spf13/cobra/issues/1816") == ("spf13", "cobra", 1816)
    assert parse_issue_url("http://github.com/go-playground/validator/issues/1576/") == (
        "go-playground", "validator", 1576,
    )
    for bad in ("not a url", "https://github.com/spf13/cobra/pull/1817"):
        try:
            parse_issue_url(bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass


def test_slug_and_branch():
    assert slugify("Completion detects non-flag argument as flag") == "completion-detects-non-flag"
    assert branch_name(1816, "Completion detects non-flag").startswith("fix/1816-")
    assert len(branch_name(99, "word " * 40)) <= 60


# --------------------------- llm: salvage / repair ---------------------------
def test_salvage_repairs_bad_json():
    fg = "<function=replace_lines>{\"path\":\"command.go\",\"new_string\":\"x == \\'-\\'\"}</function>"
    calls = _salvage_tool_calls(fg)
    assert len(calls) == 1 and calls[0].name == "replace_lines"


def test_repair_json():
    import json
    assert json.loads(_repair_json("{\"a\": \"it\\'s\"}"))["a"] == "it's"


def test_parse_content_tool_calls():
    import json
    # Some models emit the tool call as JSON in the content field instead of tool_calls.
    calls = _parse_content_tool_calls('{"name": "edit_file", "arguments": {"path": "x.go", "new_string": "b"}}')
    assert len(calls) == 1 and calls[0].name == "edit_file"
    assert json.loads(calls[0].arguments)["path"] == "x.go"
    # wrapped in code fences + "parameters" key
    c2 = _parse_content_tool_calls('```json\n{"name":"run_tests","parameters":{}}\n```')
    assert len(c2) == 1 and c2[0].name == "run_tests"
    # plain prose -> no tool calls
    assert _parse_content_tool_calls("I think the fix is complete now.") == []


# --------------------------- llm: error categorisation ---------------------------
def test_friendly_error_categories():
    assert "Authentication failed" in friendly_error(FakeErr("Invalid API key", 401))
    assert "not found" in friendly_error(FakeErr("model decommissioned", 404)).lower()
    assert "rate/quota" in friendly_error(FakeErr("rate limit reached: tokens per day", 429))
    assert "provider error" in friendly_error(FakeErr("weird boom", 500)).lower()
    assert friendly_error(None)


def test_is_rate_limit():
    assert _is_rate_limit(FakeErr("x", 429))
    assert _is_rate_limit(FakeErr("x", 413))
    assert _is_rate_limit(FakeErr("quota exceeded", None))
    assert not _is_rate_limit(FakeErr("all good", 200))
    # regression: "generated" must NOT be read as "rate" (substring bug).
    assert not _is_rate_limit(FakeErr("The model generated output that could not be parsed", 400))
    assert "rate/quota" not in friendly_error(FakeErr("The model generated output", 400))


def test_is_toolcall_error():
    assert _is_toolcall_error(FakeErr("x", body={"error": {"code": "output_parse_failed"}}))
    assert _is_toolcall_error(FakeErr("Tool call validation failed for replace_lines"))
    assert not _is_toolcall_error(FakeErr("normal message", 500))


def test_parse_retry_after():
    assert 6.0 <= _parse_retry_after(FakeErr("Please retry in 5.5s")) <= 7.0
    assert _parse_retry_after(FakeErr("retryDelay: 47s")) == 48.0
    assert _parse_retry_after(FakeErr("no hint here")) is None


# --------------------------- loop: context pruning ---------------------------
def test_prune_history_bounds_size():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "issue"}]
    for i in range(6):
        messages.append({"role": "assistant", "content": ""})
        messages.append({"role": "tool", "tool_call_id": str(i), "content": "X" * 5000})
    _prune_history(messages, max_chars=14000, keep_recent_tools=2)
    total = sum(len(str(m.get("content", ""))) for m in messages)
    assert total <= 14000
    # the two most recent tool messages stay full
    assert messages[-1]["content"] == "X" * 5000


# --------------------------- tools: scope guard + file ops ---------------------------
def test_path_scope_guard():
    with tempfile.TemporaryDirectory() as d:
        t = Tools(d)
        assert t._safe("inside.go") is not None
        assert t._safe("../outside.txt") is None
        assert t._safe("../../etc/passwd") is None


def test_read_file_range_is_capped():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "big.txt"
        p.write_text("\n".join(f"line{i}" for i in range(1, 501)), encoding="utf-8")
        t = Tools(d)
        out = t._tool_read_file("big.txt", 1, 400)
        assert len(out.split("\n")) == MAX_FILE_LINES  # 400 requested -> capped
        assert "ERROR" not in t._tool_read_file("big.txt", 1, 5)
        assert "not found" in t._tool_read_file("missing.txt")


def test_edit_file_uniqueness():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.txt"
        p.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        t = Tools(d)
        assert "applied" in t._tool_edit_file("f.txt", "beta", "BETA").lower()
        assert "BETA" in (Path(d) / "f.txt").read_text()
        assert "not found" in t._tool_edit_file("f.txt", "nope", "x").lower()
        p.write_text("dup\ndup\n", encoding="utf-8")
        assert "matches 2" in t._tool_edit_file("f.txt", "dup", "x")


def test_dispatch_normalizes_tool_name():
    with tempfile.TemporaryDirectory() as d:
        t = Tools(d)
        # hallucinated namespace should resolve to a real tool, not "unknown tool"
        assert "unknown tool" not in t.dispatch("repo_browser.list_files", {"directory": "."}).lower()
        # a genuinely unknown tool still errors helpfully (lists available tools)
        out = t.dispatch("frobnicate", {})
        assert "unknown tool" in out.lower() and "search_code" in out


def test_dispatch_arg_aliases():
    # The model sometimes uses line_start/line_end instead of start_line/end_line.
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "f.go").write_text("\n".join(f"line{i}" for i in range(1, 30)), encoding="utf-8")
        t = Tools(d)
        out = t.dispatch("read_file", {"path": "f.go", "line_start": 1, "line_end": 3})
        assert "unexpected keyword" not in out and "ERROR" not in out


def test_edit_marks_dirty():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "f.txt").write_text("a\nb\n", encoding="utf-8")
        t = Tools(d)
        t._dirty = False
        t._tool_edit_file("f.txt", "a", "X")
        assert t._dirty is True


def test_verify_uses_cache_when_clean():
    # When nothing changed since a passing run_tests, verify must NOT re-run the suite.
    with tempfile.TemporaryDirectory() as d:
        t = Tools(d)
        t._dirty = False
        t._last_pass = True
        t.has_changes = lambda: True
        def _boom(*a, **k):
            raise AssertionError("run_tests must not be called when result is cached")
        t._tool_run_tests = _boom
        ok, _reason = t.verify()
        assert ok is True


def test_fail_regex_and_baseline():
    out = "--- FAIL: TestFoo (0.00s)\n--- FAIL: TestBar/sub (0.01s)\nok pkg 0.1s"
    assert set(_FAIL_RE.findall(out)) == {"TestFoo", "TestBar/sub"}


# --------------------------- config: provider/model resolution ---------------------------
def _with_env(**kv):
    saved = {k: os.environ.get(k) for k in kv}

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return restore


def test_config_invalid_provider():
    restore = _with_env(LLM_PROVIDER="bogus")
    try:
        load_config()
        raise AssertionError("expected ValueError for bad provider")
    except ValueError:
        pass
    finally:
        restore()


def test_config_provider_override_uses_provider_default_model():
    # Override provider to gemini WITHOUT LLM_MODEL: must not leak the config.yaml groq model.
    restore = _with_env(LLM_PROVIDER="gemini", LLM_MODEL=None, GEMINI_API_KEY="testkey")
    try:
        cfg = load_config()
        assert cfg.llm.provider == "gemini"
        assert cfg.llm.model == PROVIDERS["gemini"]["default_model"]
    finally:
        restore()


# --------------------------- github / output formatting ---------------------------
def test_format_issue_context():
    ctx = format_issue_context({"title": "T", "body": "B", "comments": ["hello"]})
    assert "Title: T" in ctx and "B" in ctx and "hello" in ctx


def test_ascii_normalize():
    assert _ascii("auto‑complete – “quote”") == 'auto-complete - "quote"'


class _FakeTools:
    def __init__(self):
        self.calls = []
        self.edited = False

    def schemas(self):
        return []

    def dispatch(self, name, args):
        self.calls.append((name, args))
        if name in ("edit_file", "replace_lines"):
            self.edited = True
        return "ok"

    def verify(self):
        return (self.edited, "changes present" if self.edited else "No code changes were made.")


def test_loop_executes_tools_and_finishes():
    """The loop must run the requested tool with parsed args, then accept DONE only after
    verify() confirms a real change."""
    class FakeLLM:
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[
                    ToolCall("1", "edit_file", '{"path":"x.go","old_string":"a","new_string":"b"}')])
            return LLMResponse(content="DONE: fixed it", tool_calls=[])

    t = _FakeTools()
    res = run_agent(FakeLLM(), t, "fix the bug", max_turns=5, log=None)
    assert res["status"] == "done"
    assert ("edit_file", {"path": "x.go", "old_string": "a", "new_string": "b"}) in t.calls


def test_loop_rejects_false_done():
    """If the model claims DONE without any real change, the loop must NOT accept it."""
    class FakeLLM:
        def complete(self, messages, tools=None):
            return LLMResponse(content="DONE: all done", tool_calls=[])  # never edits

    res = run_agent(FakeLLM(), _FakeTools(), "fix the bug", max_turns=10, log=None)
    assert res["status"] == "give_up"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
