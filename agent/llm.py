"""Provider-agnostic LLM client built on the OpenAI-compatible API.

All four supported providers (groq, gemini, openai, anthropic) expose an
OpenAI-compatible chat-completions endpoint, so one client works for all.

complete() returns a normalized LLMResponse (content + tool_calls) so the loop is
decoupled from the raw SDK objects. It also handles two real-world wrinkles:
  * transient errors / rate limits -> exponential backoff (honouring retry hints);
  * Groq/Llama emitting a malformed tool call -> salvage it from failed_generation.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from openai import OpenAI

try:  # available in openai>=1.x
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        BadRequestError,
        InternalServerError,
        RateLimitError,
    )
except Exception:  # pragma: no cover
    APIConnectionError = APITimeoutError = InternalServerError = RateLimitError = Exception
    BadRequestError = APIStatusError = Exception

# Matches the non-standard tool call some models (Llama on Groq) emit as text.
_FUNC_RE = re.compile(r"<function=([A-Za-z0-9_]+)>\s*(\{.*?\})\s*</function>", re.DOTALL)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string


@dataclass
class LLMResponse:
    content: str
    tool_calls: list


def _parse_retry_after(err):
    s = str(err)
    m = re.search(r"retry in ([\d.]+)s", s) or re.search(r"retryDelay['\"]?:?\s*['\"]?(\d+)s", s)
    if m:
        return min(float(m.group(1)) + 1.0, 65.0)
    return None


def _err_code(err):
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        return (body.get("error") or {}).get("code")
    return None


def _is_toolcall_error(err):
    """True for provider errors caused by the model mis-formatting a tool call."""
    if _err_code(err) in ("output_parse_failed", "tool_use_failed"):
        return True
    s = str(err).lower()
    return "tool call validation" in s or "did not match schema" in s or "tool_use_failed" in s


def _status_code(err):
    return getattr(err, "status_code", None)


def _is_rate_limit(err):
    low = str(err).lower()
    # Note: match "rate limit" (not bare "rate" — that matches "generated"!).
    return (
        _status_code(err) in (429, 413)
        or "rate limit" in low or "rate_limit" in low or "ratelimit" in low
        or "quota" in low or "tokens per" in low or "too many requests" in low
    )


def friendly_error(err):
    """A clear, actionable one-line message for a provider error."""
    if err is None:
        return "LLM call failed for an unknown reason."
    code = _status_code(err)
    s = str(err)
    low = s.lower()
    if code in (401, 403) or "invalid api key" in low or "unauthorized" in low:
        return ("Authentication failed - check your API key (and that it can access the model). "
                f"Provider said: {s[:160]}")
    if code == 404 or "model_not_found" in low or "does not exist" in low or "decommissioned" in low:
        return f"Model/endpoint not found - check LLM_MODEL and LLM_PROVIDER. Provider said: {s[:160]}"
    if _is_rate_limit(err):
        return ("Free-tier rate/quota limit reached. Wait for it to reset, or switch "
                "LLM_MODEL / LLM_PROVIDER in .env (e.g. a different free tier, or a paid "
                f"OpenAI/Anthropic key). Provider said: {s[:180]}")
    return f"LLM provider error: {s[:200]}"


def _extract_failed_generation(err):
    """Pull Groq's failed_generation (the malformed tool call) out of a 400 error."""
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        fg = body.get("error", {}).get("failed_generation")
        if fg:
            return fg
    m = re.search(r"failed_generation['\"]?:\s*['\"](.+?)['\"]\s*\}\s*\}", str(err), re.DOTALL)
    return m.group(1) if m else None


def _repair_json(raw):
    """Drop invalid backslash escapes (e.g. \\' which some models emit) so the
    args become valid JSON. JSON only allows \\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX."""
    return re.sub(r'\\([^"\\/bfnrtu])', r"\1", raw)


def _salvage_tool_calls(failed_generation):
    calls = []
    for i, m in enumerate(_FUNC_RE.finditer(failed_generation or "")):
        name, raw = m.group(1), m.group(2)
        args = None
        for candidate in (raw, _repair_json(raw)):
            try:
                json.loads(candidate)
                args = candidate
                break
            except Exception:
                continue
        if args is None:
            continue
        calls.append(ToolCall(id=f"salvaged_{i}", name=name, arguments=args))
    return calls


def _parse_content_tool_calls(content):
    """Recover tool calls that a model emitted as JSON in the content field instead of the
    structured tool_calls field (some models do this). Looks for objects like
    {"name": "edit_file", "arguments": {...}} (or "parameters")."""
    if not content:
        return []
    s = content.strip()
    s = re.sub(r"```(?:json)?", "", s)
    s = s.replace("<tool_call>", "").replace("</tool_call>", "").strip()

    candidates = []
    try:
        obj = json.loads(s)
        candidates = obj if isinstance(obj, list) else [obj]
    except Exception:
        # Find individual {...} objects (one level of nesting for the args).
        for m in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", s):
            try:
                candidates.append(json.loads(m.group(0)))
            except Exception:
                continue

    calls = []
    for i, o in enumerate(candidates):
        if not isinstance(o, dict) or "name" not in o:
            continue
        args = o.get("arguments", o.get("parameters", {}))
        if isinstance(args, dict):
            args = json.dumps(args)
        elif not isinstance(args, str):
            args = "{}"
        calls.append(ToolCall(id=f"content_{i}", name=str(o["name"]), arguments=args))
    return calls


class LLMClient:
    def __init__(self, settings, temperature: float = 0.0):
        self.client = OpenAI(base_url=settings.base_url, api_key=settings.api_key)
        self.model = settings.model
        self.temperature = temperature

    def complete(self, messages, tools=None, max_retries: int = 8) -> LLMResponse:
        delay = 2.0
        last_err = None
        temp = self.temperature
        toolcall_fails = 0
        for attempt in range(max_retries):
            try:
                kwargs = dict(model=self.model, messages=messages, temperature=temp)
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if "gpt-oss" in self.model:
                    # gpt-oss uses a "harmony" reasoning format that intermittently fails
                    # to parse on Groq; low reasoning effort reduces those failures and
                    # token usage without hurting these focused fixes.
                    kwargs["extra_body"] = {"reasoning_effort": "low"}
                resp = self.client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                tool_calls = []
                if msg.tool_calls:
                    tool_calls = [
                        ToolCall(tc.id, tc.function.name, tc.function.arguments)
                        for tc in msg.tool_calls
                    ]
                content = msg.content or ""
                # Some models put the tool call as JSON in the content field instead of
                # the structured tool_calls field. Recover it so they still work.
                if not tool_calls and content:
                    parsed = _parse_content_tool_calls(content)
                    if parsed:
                        return LLMResponse(content="", tool_calls=parsed)
                return LLMResponse(content=content, tool_calls=tool_calls)
            except BadRequestError as e:
                # Salvage a malformed tool call (Groq/Llama) instead of failing.
                salvaged = _salvage_tool_calls(_extract_failed_generation(e))
                if salvaged:
                    return LLMResponse(content="", tool_calls=salvaged)
                # Unparseable / failed tool call with nothing to salvage: retry with a
                # higher temperature to escape a deterministic malformed generation.
                if _is_toolcall_error(e):
                    last_err = e
                    toolcall_fails += 1
                    # Parse/tool-call errors rarely recover; give up after a few tries
                    # instead of burning the full retry budget (avoids long stalls).
                    if toolcall_fails >= 4 or attempt == max_retries - 1:
                        break
                    temp = 0.5
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise RuntimeError(friendly_error(e))  # genuine bad request -> clear message
            except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as e:
                last_err = e
                if attempt == max_retries - 1:
                    break
                wait = _parse_retry_after(e)
                if wait is None:
                    wait = delay
                    delay = min(delay * 2, 30)
                time.sleep(wait)
            except APIStatusError as e:
                # Retry rate/size limits; fail fast (with a clear message) on auth/404/etc.
                if not _is_rate_limit(e):
                    raise RuntimeError(friendly_error(e))
                last_err = e
                if attempt == max_retries - 1:
                    break
                wait = _parse_retry_after(e)
                if wait is None:
                    wait = delay
                    delay = min(delay * 2, 30)
                time.sleep(wait)
        raise RuntimeError(friendly_error(last_err))
