# cobra #1816 — agent output vs. accepted PR #1817

**Issue:** [spf13/cobra#1816](https://github.com/spf13/cobra/issues/1816) — "Completion
detects non-flag argument as flag"
**Accepted human fix:** [PR #1817](https://github.com/spf13/cobra/pull/1817)
**Base commit (bug present):** `bf11ab6` (parent of the PR's merge commit)
**Provenance:** a live agent run — provider Groq `openai/gpt-oss-120b`, status `done`,
6 steps, validated by `go test ./...`. See `run.log`.

## The accepted PR (#1817)
- `command.go`: `arg[1] == '-'`  →  `arg[0:2] == "--"`  (one line)
- `completions_test.go`: +64 lines — a regression test (`TestArgsNotDetected...`)

## The agent's change (`patch.diff`)
- `command.go`: `arg[1] == '-'`  →  `arg[0] == '-' && arg[1] == '-'`  (one line)

`arg[0] == '-' && arg[1] == '-'` is **logically identical** to the PR's `arg[0:2] == "--"`:
both require the argument to start with `--` before treating it as a long flag — exactly
the fix. The agent's diff is the same single line the human changed.

## Evaluation on the five axes
| Axis | Result |
|------|--------|
| Identifies the right files | ✅ `command.go` — the same file the PR changes |
| Produces relevant code changes | ✅ Correct, minimal, logically equivalent to the accepted fix |
| Follows project conventions | ✅ gofmt-clean; preserves the existing short-flag branch |
| Runs appropriate validation | ✅ `go test ./...` in-loop; passes (only failure is the pre-existing, Windows-only `TestFailGenFishCompletionFile`, ignored via the baseline) |
| Generates a reasonable PR summary | ✅ see `pr.md` (Summary / Root cause / Fix / Testing) |

## Honest gap
The accepted PR also adds a **regression test**; this run fixed and validated the code but
did not add a new test file (it passed existing tests, and verify-on-termination secured a
clean `done`). The agent's `pr.md` "Testing" section describes the reproducer conceptually
rather than committing a new `_test.go` case. The code fix itself matches the human change.
