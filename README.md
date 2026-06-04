# Agentic AI Contributor for Open-Source Go Projects

An agentic system that takes a **GitHub issue** from an approved Go repository, locates the
relevant code, fixes it, runs the tests, and produces a **branch + patch + PR summary**.

It is a real **LLM + tools + loop** framework — not a one-shot prompt or a thin wrapper.
The LLM is the "brain"; the system around it (tools, repository understanding, validation,
recovery) is the "body" that lets it read, edit, and verify code on its own until the issue
is fixed.

---

## Table of contents
1. [How it works](#how-it-works)
2. [Approved repositories & validated issues](#approved-repositories--validated-issues)
3. [Quick start](#quick-start) — Docker **or** native
4. [Choosing your LLM (providers & keys)](#choosing-your-llm)
5. [A note on agent quality vs. the model you choose](#a-note-on-agent-quality-vs-the-model-you-choose)
6. [How to use](#how-to-use)
7. [The prompts](#the-prompts)
8. [Design & reliability notes](#design--reliability-notes)
9. [Project layout](#project-layout)
10. [Tests](#tests)
11. [Sample outputs](#sample-outputs)
12. [Honest limitations](#honest-limitations)

---

## How it works

```
issue URL ──▶ fetch the issue (title + body + top comments)        [github.py]
          ──▶ clone the repo at the PRE-FIX commit, make a branch   [repo.py]
          ──▶ establish a test baseline (ignore pre-existing fails) [tools.py]
          ──▶ AGENT LOOP:  search → read → edit → run tests         [loop.py + tools.py]
              (the harness VERIFIES a real diff + passing tests
               before it will accept the model's "DONE")
          ──▶ emit patch.diff + pr.md + run.log                      [output.py]
          ──▶ (optional --open-pr) push a branch + open a PR on your fork
```

The agent works by calling **tools** (the only intelligence the loop adds is shuttling
tool requests/results to and from the LLM):

| Tool | What it does |
|------|--------------|
| `search_code` | `git grep` across the repo (find functions, identifiers, error strings) |
| `read_file` | read a file, optionally a line range (ranged reads keep context small) |
| `list_files` | list tracked files |
| `edit_file` | exact-string replace (auto-`gofmt` on `.go` files) |
| `replace_lines` | line-number-based replace (auto-`gofmt`) |
| `run_tests` | `go test`, with pre-existing/OS-specific failures filtered out |
| `run_build` | `go build ./...` |

None of the tools are repo-specific, so the engine works on **any** Go repo. The approved
set is enforced via `config.yaml`.

---

## Approved repositories & validated issues

Approved set (assignment): `gin-gonic/gin`, `spf13/cobra`, `go-playground/validator`,
`golangci/golangci-lint`. We focused on **cobra** (+ one validator issue) and pinned the
**pre-fix commit** of each target so the bug is present when the agent runs.

| Issue | Accepted PR | Type |
|-------|-------------|------|
| `spf13/cobra#1816` | #1817 | completion misreads `1-123` as a flag |
| `spf13/cobra#1651` | #1776 | shadowed persistent flag missing from help |
| `spf13/cobra#1777` | #1781 | flag value matching a subcommand name |
| `go-playground/validator#1576` | #1577 | cron regex missing `^…$` anchors |

A fully verified, committed run for **cobra #1816** is in `samples/cobra-1816/`.

---

## Quick start

### 0. Get an API key (Groq — free, ≈2 minutes, no credit card)

1. Go to **https://console.groq.com** → sign in (Google/GitHub).
2. **API Keys** (left sidebar) → **Create API Key** → name it → **Submit**.
3. Copy the key (it starts with `gsk_`) — Groq shows it **only once**, so save it.

You'll use it below as `GROQ_API_KEY`. Prefer a different or paid provider (Gemini / OpenAI /
Anthropic)? See [Choosing your LLM](#choosing-your-llm) for each one's key + setup.

Then pick **one** of the two paths below.

### Path A — Docker (recommended; nothing installed on your host)

Requires only **Docker**. The image bundles Go + Python + git; everything installs *inside*
the image, so your machine stays clean.

```bash
docker build -t go-contributor .

# Linux/macOS:
docker run --rm -e GROQ_API_KEY=<your-key> -v "${PWD}/out:/app/out" \
  go-contributor run --issue <github-issue-url>

# Windows PowerShell:
docker run --rm -e GROQ_API_KEY=<your-key> -v "${PWD}\out:/app/out" `
  go-contributor run --issue <github-issue-url>
```

Replace `<github-issue-url>` with any approved-repo issue. To try a **known-good** one, use our
verified test issue **`https://github.com/spf13/cobra/issues/1816`** — outputs then land in
`./out/cobra-1816/` (mounted from the container).

### Path B — Native (install deps yourself, then run)

**Prerequisites:** Python 3.11+, Go (1.21+), git. (Optional: `gh` CLI, only for `--open-pr`.)

```bash
# 1. clone your submission repo and enter it
cd agentic-go-contributor

# 2. create a virtualenv and install Python deps
python -m venv .venv
# Windows:  .venv\Scripts\activate
# bash:     source .venv/bin/activate
pip install -r requirements.txt

# 3. configure your LLM key
cp .env.example .env        # Windows: Copy-Item .env.example .env
#   then edit .env and paste your GROQ_API_KEY (or another provider's key)

# 4. run it (replace the URL with any approved-repo issue)
python -m agent run --issue <github-issue-url>
#   known-good test issue:
#   python -m agent run --issue https://github.com/spf13/cobra/issues/1816
```

---

## Choosing your LLM

The provider is **allowlisted** (4 options); the model is any string that provider accepts.
All four use an OpenAI-compatible endpoint, so one client drives them all. Set the provider
and the matching key in `.env`.

| Provider | Cost | Key env var | Default model | Where to get a key |
|----------|------|-------------|---------------|--------------------|
| **`groq`** (default) | **free** | `GROQ_API_KEY` | `openai/gpt-oss-120b` | https://console.groq.com (no card) |
| `gemini` | free | `GEMINI_API_KEY` | `gemini-2.5-flash` | https://aistudio.google.com/app/apikey |
| `openai` | paid | `OPENAI_API_KEY` | `gpt-4o` | https://platform.openai.com/api-keys |
| `anthropic` | paid | `ANTHROPIC_API_KEY` | `claude-3-5-sonnet-latest` | https://console.anthropic.com |

### Getting an API key

Groq's free key is covered in [Quick start → step 0](#quick-start). For the **other** providers,
create a key in that provider's console, then set the matching key env var + `LLM_PROVIDER` in `.env`:

| Provider | Where to create the key | Looks like | Set in `.env` |
|----------|-------------------------|------------|----------------|
| Gemini (free) | https://aistudio.google.com/app/apikey → **Create API key** | `AIza...` | `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=...` |
| OpenAI (paid) | https://platform.openai.com/api-keys → **Create new secret key** | `sk-...` | `LLM_PROVIDER=openai` and `OPENAI_API_KEY=...` |
| Anthropic (paid) | https://console.anthropic.com → **Settings → API Keys → Create Key** | `sk-ant-...` | `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...` |

> You only need a key for the **one** provider you choose. Optionally set `LLM_MODEL` for a
> specific model; otherwise the provider's default (in the first table) is used.

**To use the free default (Groq):**
```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...your-key...
```

**To use a paid key (recommended for best results), e.g. OpenAI:**
```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o            # or any model you have access to
OPENAI_API_KEY=sk-...your-key...
```

**To use a specific model on any provider**, set `LLM_MODEL` (it always wins). With Docker,
pass these as `-e` flags instead of editing `.env`:
```bash
docker run --rm -e LLM_PROVIDER=anthropic -e ANTHROPIC_API_KEY=<key> \
  -e LLM_MODEL=claude-3-5-sonnet-latest -v "${PWD}/out:/app/out" \
  go-contributor run --issue <url>
```

The harness gives a **clear message** (not a stack trace) if a key is missing, a provider
is unknown, or a rate limit is hit.

---

## A note on agent quality vs. the model you choose

This is a **framework around an LLM**: the harness (tools, validation, recovery) is fixed,
but **the success rate scales with the model you plug in.** From our own testing:

- **Independent of the model**, the framework reliably **identifies the right files/function**,
  **runs validation**, and **generates a PR summary** on every issue we tried.
- **Completing** the fix end-to-end depends on the model's coding strength:
  - **Strong models** (GPT-4 / Claude 3.5+ / Gemini 2.5 Pro) execute multi-line edits cleanly
    and reliably solve small/medium issues.
  - The **free Groq `gpt-oss-120b`** reliably solves the simplest, single-line-change issues
    (e.g. cobra #1816 — see `samples/`) but can struggle to *execute* subtler multi-line fixes,
    even when it localizes them correctly (and free tiers also rate-limit).

**Recommendation:** for the best results, use a **capable model** — a paid OpenAI/Anthropic key,
or Gemini 2.5 Pro. The free Groq default is perfect for trying the system and for focused issues.
This trade-off is by design, and matches the assignment's note that a *thoughtful framework that
solves focused issues reliably* beats a complex/unreliable one.

---

## How to use

```bash
python -m agent run --issue <github-issue-url> [options]
```

> **Try it on a verified easy issue:** `https://github.com/spf13/cobra/issues/1816` — this is
> the issue we tested end-to-end; it runs to a clean `DONE`, and its result is committed in
> `samples/cobra-1816/`.

Options:
- `--base-sha <sha>` — start from a specific commit (the pre-fix state). Auto-detected for
  known demo issues; falls back to the default branch otherwise.
- `--allow-any` — operate on a repo outside the approved list.
- `--open-pr` — also push the branch and open a PR **on your own fork** (never upstream).
  Requires the `gh` CLI (`gh auth login`) or `GH_TOKEN`.

Every run writes to `out/<repo>-<number>/`:
- **`patch.diff`** — the change the agent made
- **`pr.md`** — generated PR title + body
- **`run.log`** — full transcript (every tool call + result), for auditing

There is also a developer entry point to run the agent against an already-cloned repo + a
local issue text file (used while building/testing the loop):
```bash
python -m agent dev-run --repo-path .work/cobra --issue-file testdata/cobra-1816.txt
```

---

## The prompts

The agent is steered by the system prompt in **`agent/prompts.py`**. Its core (abridged):

```text
You are an expert Go engineer working as an autonomous open-source contributor.
You are given a GitHub issue from a Go repository that is already checked out locally.
Your job is to FIX the issue with a minimal, correct, production-quality change.

The ONLY tools that exist are: search_code, read_file, list_files, edit_file,
replace_lines, run_tests, run_build. Never invent or namespace a tool name.

1. Understand the issue (bug, expected vs actual behaviour).
2. IMMEDIATELY search_code for the most specific identifier named in the issue. The file
   where it is found is the file you will edit.
3. ALWAYS read_file the relevant code BEFORE editing it; never edit a file you have not read.
4. Make the SMALLEST change that fixes the bug. Prefer replace_lines with exact line numbers;
   your new_string must replace ONLY those lines with balanced braces. Do NOT delete adjacent
   functions/comments/logic.
5. Add or update a test that covers the bug (encouraged, optional).
6. Validate with run_tests (pre-existing / OS-specific failures are ignored automatically).
7. Once tests pass, reply with a short summary beginning with "DONE:".
   If you cannot solve it, reply beginning with "GIVE_UP:".
```

A second prompt (`PR_SUMMARY_PROMPT`) turns the final diff into a PR title + body.

---

## Design & reliability notes

Real-world hardening that makes focused issues solve *reliably* (each is small + tested):

- **Pre-fix commit pinning** — clone at the commit *before* the fix so the bug is present;
  reproducible per issue.
- **Test baseline** — record tests that already fail on the pristine repo (e.g. an OS-specific
  one on Windows) and ignore them, so the validation signal is accurate.
- **Completion gate** — the harness verifies a real diff exists *and* tests pass before it
  accepts "DONE"; the model cannot over-claim success (and a validated fix still counts even if
  the model stumbles on a later step).
- **Auto-`gofmt`** on every Go edit (+ syntax-error feedback) — output follows conventions.
- **Robust tool calls** — recovers tool calls that some models mis-format (text/JSON instead of
  structured), normalizes hallucinated tool/param names, retries transient parse errors.
- **Context management** — ranged reads + history pruning keep requests within tight token caps.
- **Graceful errors** — rate limits, auth failures, and broken clones produce a *clear message*,
  never a crash; the pipeline verifies the clone/checkout actually succeeded.
- **Guardrails** — turn cap, anti-repeat guard, and a path scope guard (no writes outside the repo).

---

## Project layout

```
agent/
  __main__.py   cli.py       # `python -m agent` ; run / dev-run commands
  pipeline      github.py    repo.py    output.py   # fetch / clone / artifacts (deterministic)
  loop.py       tools.py                            # the agent loop + its tools (the "heart")
  llm.py        prompts.py                          # provider-agnostic client + prompts
  pr.py         runlog.py    config.py              # optional fork PR / logging / config
config.yaml   .env.example   Dockerfile   requirements.txt
tests/        testdata/      samples/
```

---

## Tests

Unit tests for our own logic (no network / no LLM / no Go needed) — run from the project root:

```bash
python tests/test_basics.py        # or: python -m pytest tests
```

They cover: URL parsing, error categorization, rate-limit & tool-call detection, the salvage /
content-tool-call parsers, context pruning, the path scope guard, file-read capping, edit
uniqueness, the test-result cache, config provider/model resolution, **and the agent loop itself**
(executing tools, and rejecting a hollow "DONE").

---

## Sample outputs

`samples/<repo>-<issue>/` holds a full run with a comparison to the real accepted PR:

```
samples/cobra-1816/
  patch.diff      # the agent's change (verified: compiles, gofmt-clean, tests pass)
  pr.md           # generated PR title + body
  run.log         # full transcript
  COMPARISON.md   # agent's diff vs the real PR, on the 5 evaluation axes
```

cobra #1816 was solved end-to-end with a clean `DONE` on the free Groq model; its fix is
**logically identical to the accepted PR #1817**.

---

## Honest limitations

- This is **not** a production-grade autonomous agent (by design — see the assignment note). It
  targets **small/medium, localized** issues; large architectural changes are out of scope.
- On **free** models, end-to-end success is most reliable on the simplest issues; harder fixes
  benefit from a stronger model (see [the note above](#a-note-on-agent-quality-vs-the-model-you-choose)).
- Adding a regression test is encouraged but optional; the agent prioritizes a validated fix.
