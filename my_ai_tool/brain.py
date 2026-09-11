"""The Brain — talks to the LLM API.

Providers:
    ollama  (default)  http://localhost:11434  — free, local, no API key
    openai             any OpenAI-compatible /chat/completions endpoint
    mock               offline test provider (used by selftests / demos)

All answers are requested in JSON mode and parsed robustly (code fences,
prose around JSON, etc.).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from . import config
from .logging_setup import get_logger

log = get_logger("brain")


class BrainError(RuntimeError):
    pass


PLANNER_SYSTEM = """You are 'mytool', an autonomous terminal agent running on a Linux machine.
You get a natural-language task and must produce shell commands that accomplish it.
Rules:
- Reply with ONLY valid JSON, no markdown fences: {"explanation": "...", "commands": ["cmd1", "cmd2"]}
- Commands run sequentially in bash, non-interactively, with a timeout.
- Prefer safe, reversible commands. Never use sudo unless explicitly asked.
- Prefer creating files under the user's home directory.
- If no command is needed, return {"explanation": "...", "commands": []}.
"""

FOLLOWUP_SYSTEM = """You are 'mytool', an autonomous terminal agent on Linux.
Previous commands were executed and some failed (or the task is incomplete).
Reply with ONLY valid JSON, either:
  {"commands": ["next command", ...]}                     to continue fixing it, or
  {"done": true, "summary": "what happened"}              to give up / finish.
"""

HEALER_SYSTEM = """You are a Python self-repair agent for the 'mytool' project.
You receive: a crash traceback, the suspected source file path, and its current
full content. Diagnose the bug and rewrite the whole file, fixed.
Rules:
- Reply ONLY valid JSON, no markdown fences:
  {"analysis": "...", "file_to_fix": "<path exactly as given>", "full_corrected_code": "<entire corrected file>"}
- Fix only the bug; keep everything else identical.
- Use only the Python standard library (no new third-party imports).
- Keep the public function/class names and CLI behaviour unchanged.
"""


# --------------------------------------------------------------- JSON utils

def extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            pass
    raise BrainError("LLM reply is not valid JSON:\n" + text[:500])


# ---------------------------------------------------------------- providers

def chat(messages: list, json_mode: bool = True) -> dict:
    cfg = config.load_config()
    provider = cfg.get("provider", "ollama")
    last_err = None
    for attempt in (1, 2):  # one retry on transient network errors
        try:
            if provider == "ollama":
                return _ollama(messages, cfg, json_mode)
            if provider == "openai":
                return _openai(messages, cfg, json_mode)
            if provider == "mock":
                return _mock(messages)
            raise BrainError(f"Unknown provider: {provider!r}")
        except BrainError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            log.warning("brain attempt %d failed: %s", attempt, e)
            time.sleep(1.5)
    raise BrainError(f"LLM ({provider}) unreachable after retries: {last_err}")


def _ollama(messages: list, cfg: dict, json_mode: bool) -> dict:
    o = cfg.get("ollama", {})
    url = o.get("url", "http://localhost:11434").rstrip("/") + "/api/chat"
    model = o.get("model") or _ollama_first_model(o.get("url", "http://localhost:11434"))
    timeout = int(o.get("timeout_sec", 180))
    payload = {"model": model, "messages": messages, "stream": False}
    if json_mode:
        payload["format"] = "json"
    try:
        resp = _http_json(url, payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise BrainError(f"Ollama HTTP {e.code}: check model name {model!r}") from e
    content = (resp.get("message") or {}).get("content", "")
    return extract_json(content)


def _ollama_first_model(base_url: str) -> str:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=10) as r:
            models = json.loads(r.read().decode("utf-8")).get("models", [])
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        raise BrainError(
            f"Ollama not reachable at {base_url} — is it running? "
            "Start it with `ollama serve` and pull the model: `ollama pull hermes3:3b`"
            " (ya auto-setup: `mytool setup-ollama`)")
    if not models:
        raise BrainError(
            "Ollama has no models installed. Run: `ollama pull hermes3:3b`"
            " (ya auto-setup: `mytool setup-ollama`)")
    return models[0].get("name", "hermes3:3b")


def _openai(messages: list, cfg: dict, json_mode: bool) -> dict:
    o = cfg.get("openai", {})
    import os
    key = os.environ.get(o.get("api_key_env", "OPENAI_API_KEY"), "")
    if not key:
        raise BrainError(f"Set the API key env var {o.get('api_key_env')!r} first.")
    url = o.get("base_url", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    payload = {"model": o.get("model", "gpt-4o-mini"), "messages": messages,
               "temperature": 0.2}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return extract_json(resp["choices"][0]["message"]["content"])


def _http_json(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# -------------------------------------------------------------- mock (tests)

def _mock(messages: list) -> dict:
    """Deterministic offline provider for testing the whole pipeline."""
    system = (messages[0].get("content", "") if messages else "")
    text = "\n".join(m.get("content", "") for m in messages)

    if "self-repair agent" in system:
        m = re.search(r"SUSPECT_FILE: (.+)", text)
        path = m.group(1).strip() if m else "unknown.py"
        cm = re.search(r"```python\n(.*?)```", text, re.S)
        if not cm:
            raise BrainError("mock: no code block in heal prompt")
        code = cm.group(1)
        if "ZeroDivisionError" in text:
            fixed = code.replace("1 / b", "1 / b if b else float('inf')")
            if fixed == code:
                fixed = code.replace("1/b", "1 / b if b else float('inf')")
            if fixed == code:
                raise BrainError("mock: could not produce a fix for this bug")
            return {"analysis": "mock: guarded division by zero",
                    "file_to_fix": path, "full_corrected_code": fixed}
        raise BrainError("mock: only ZeroDivisionError demo bugs can be healed")

    return {"explanation": "mock provider: acknowledged task",
            "commands": ["echo 'mock executed task'"]}


# ------------------------------------------------------------ health check

def ping() -> tuple:
    """(ok, detail) — used by `mytool selftest` and `mytool status`."""
    cfg = config.load_config()
    provider = cfg.get("provider", "ollama")
    if provider == "mock":
        return True, "mock provider (offline testing mode)"
    if provider == "openai":
        import os
        ok = bool(os.environ.get(cfg.get("openai", {}).get("api_key_env", ""), ""))
        return ok, "openai API key " + ("found" if ok else "NOT set in env")
    try:
        model = _ollama_first_model(cfg.get("ollama", {}).get("url", "http://localhost:11434"))
        return True, f"ollama reachable, model: {model}"
    except BrainError as e:
        return False, str(e)
