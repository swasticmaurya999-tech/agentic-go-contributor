"""Configuration loading: merges config.yaml with .env / environment variables.

Environment variables always override config.yaml. Secrets (API keys) live ONLY
in the environment (.env), never in config.yaml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env (if present) into the process environment.
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

# The only providers we support. Provider is allowlisted; the model within a provider
# is free-form (any string the provider accepts). All providers use an OpenAI-compatible
# endpoint, so one client works for all. Two are free (groq, gemini) and two are paid
# (openai, anthropic) — the reviewer uses whichever API key they have.
PROVIDERS = {
    "groq": {  # free tier; default
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "default_model": "openai/gpt-oss-120b",
    },
    "gemini": {  # free tier
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash",
    },
    "openai": {  # paid
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "anthropic": {  # paid
        "base_url": "https://api.anthropic.com/v1/",
        "key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-3-5-sonnet-latest",
    },
}


@dataclass
class LLMSettings:
    provider: str
    model: str
    base_url: str
    api_key: str


@dataclass
class Config:
    llm: LLMSettings
    max_turns: int
    temperature: float
    approved_repos: list
    work_dir: Path
    out_dir: Path
    demo_base_shas: dict


def _load_yaml() -> dict:
    path = ROOT / "config.yaml"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def load_config() -> Config:
    raw = _load_yaml()
    llm_raw = raw.get("llm", {}) or {}
    agent_raw = raw.get("agent", {}) or {}
    paths_raw = raw.get("paths", {}) or {}

    config_provider = (llm_raw.get("provider") or "gemini").strip().lower()
    provider = (os.getenv("LLM_PROVIDER") or config_provider).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. Allowed: {', '.join(PROVIDERS)}."
        )
    pinfo = PROVIDERS[provider]

    # Only honour the config.yaml model if it was written for THIS provider. If the provider
    # is overridden via LLM_PROVIDER, fall back to that provider's default (so e.g. a Groq
    # model name never leaks to Gemini). LLM_MODEL always wins when set.
    config_model = llm_raw.get("model") if provider == config_provider else None
    model = os.getenv("LLM_MODEL") or config_model or pinfo["default_model"]

    api_key = os.getenv(pinfo["key_env"], "")
    if not api_key:
        raise ValueError(
            f"Provider '{provider}' requires {pinfo['key_env']} to be set. "
            f"Add it to your .env file (copy .env.example to .env)."
        )

    llm = LLMSettings(
        provider=provider,
        model=model,
        base_url=pinfo["base_url"],
        api_key=api_key,
    )

    return Config(
        llm=llm,
        max_turns=int(agent_raw.get("max_turns", 25)),
        temperature=float(agent_raw.get("temperature", 0.0)),
        approved_repos=list(raw.get("approved_repos", []) or []),
        work_dir=ROOT / paths_raw.get("work_dir", ".work"),
        out_dir=ROOT / paths_raw.get("out_dir", "out"),
        demo_base_shas=dict(raw.get("demo_base_shas", {}) or {}),
    )
