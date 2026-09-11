"""One-command brain setup: Ollama server + Hermes (Nous Research) model.

    mytool setup-ollama                     # RAM dekhkar hermes3:3b/8b/70b choose karta hai
    mytool setup-ollama --size 8b
    mytool setup-ollama --model hermes3:3b

Steps:
  1. Ollama server reachable? nahi -> binary check -> missing to official installer
  2. server detached start (`ollama serve`) + /api/tags par wait
  3. model installed? nahi -> `ollama pull <model>`
  4. config save: provider=ollama, ollama.model=<model>
  5. live JSON test-generation (real brain check)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request

from . import brain, config, paths
from .logging_setup import get_logger

log = get_logger("ollama_setup")

HERMES_FAMILY = {
    "3b": "hermes3:3b",     # ~2GB download, ~3GB RAM  (chhote servers / VMs)
    "8b": "hermes3:8b",     # ~4.7GB download, ~6GB RAM (recommended)
    "70b": "hermes3:70b",   # ~40GB, bahut bade servers
}


# ------------------------------------------------------------------- helpers

def _http_json(url: str, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def server_url() -> str:
    return config.load_config().get("ollama", {}).get(
        "url", "http://localhost:11434").rstrip("/")


def server_alive() -> bool:
    try:
        _http_json(server_url() + "/api/tags")
        return True
    except Exception:
        return False


def installed_models() -> list:
    try:
        return [m.get("name", "")
                for m in _http_json(server_url() + "/api/tags").get("models", [])]
    except Exception:
        return []


def total_ram_gb():
    try:
        if hasattr(os, "sysconf"):
            return (os.sysconf("SC_PAGE_SIZE") *
                    os.sysconf("SC_PHYS_PAGES")) / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        pass
    if os.name == "nt":
        try:
            import ctypes

            class _MEM(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = _MEM()
            m.dwLength = ctypes.sizeof(_MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 1024 ** 3
        except Exception:
            return None
    return None


def suggest_size() -> str:
    gb = total_ram_gb()
    if gb is None:
        return "8b"
    if gb >= 40:
        return "70b"
    if gb >= 7:
        return "8b"
    return "3b"


# -------------------------------------------------------------- setup steps

def install_ollama() -> bool:
    if os.name == "nt":
        print("      Windows: https://ollama.com/download se installer chalao,"
              " phir dobara `mytool setup-ollama`")
        return False
    cmd = "curl -fsSL https://ollama.com/install.sh | sh"
    print(f"      $ {cmd}")
    try:
        return subprocess.run(cmd, shell=True).returncode == 0
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.warning("ollama installer failed: %s", e)
        return False


def ensure_server() -> bool:
    """Server start karo (agar binary hai) aur ready hone tak wait karo."""
    ollama = shutil.which("ollama")
    if not ollama:
        return False
    log_dir = paths.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_dir / "ollama-serve.log", "ab")
    env = dict(os.environ)
    env.setdefault("OLLAMA_HOST", "127.0.0.1:11434")
    subprocess.Popen([ollama, "serve"], stdout=log_fh, stderr=log_fh,
                     start_new_session=True)
    print("      `ollama serve` start kiya (background), wait...")
    for _ in range(30):
        time.sleep(1)
        if server_alive():
            return True
    return False


def ensure_model(model: str) -> bool:
    models = installed_models()
    if model in models:
        return True
    ollama = shutil.which("ollama")
    if not ollama:
        print("      `ollama` CLI nahi mila — model pull ke liye chahiye")
        return False
    print(f"      $ ollama pull {model}   (size ke hisaab se time lagega)")
    try:
        rc = subprocess.run([ollama, "pull", model]).returncode
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.warning("pull failed: %s", e)
        return False
    return rc == 0 and model in installed_models()


def test_generation() -> tuple:
    """Live LLM check — real HTTP call with the configured model."""
    try:
        resp = brain.chat([{"role": "user",
                            "content": 'Reply with exactly this JSON: {"ok": true}'}])
        return ("ok" in resp), json.dumps(resp)[:160]
    except brain.BrainError as e:
        return False, str(e)[:160]


# --------------------------------------------------------------------- main

def run_setup(model: str | None = None, size: str = "auto",
              skip_install: bool = False) -> int:
    print("== mytool brain setup: Ollama + Hermes (Nous Research) ==")

    # 1. server
    if server_alive():
        print(f"[1/5] Ollama server already running: {server_url()}")
    else:
        print(f"[1/5] Ollama server {server_url()} par nahi mila...")
        if not skip_install and not shutil.which("ollama"):
            print("      ollama binary missing — installer chalata hoon")
            if not install_ollama():
                print("      install fail. Manual: curl -fsSL https://ollama.com/install.sh | sh")
                return 1
        if not ensure_server():
            print("      server start nahi hua — manually `ollama serve` chalakar dobara try karo")
            return 1
        print(f"[1/5] Ollama server OK: {server_url()}")

    # 2. model choice
    if model:
        chosen = model
    else:
        if size == "auto":
            size = suggest_size()
            gb = total_ram_gb()
            ram = f"{gb:.1f}GB" if gb else "unknown"
            print(f"      RAM {ram} -> size {size} auto-chosen")
        chosen = HERMES_FAMILY.get(size, "hermes3:8b")
    print(f"[2/5] target model: {chosen}")

    # 3. pull
    if ensure_model(chosen):
        print(f"[3/5] model ready: {chosen}")
    else:
        print("      pull fail — tags: https://ollama.com/library/hermes3"
              " (3b | 8b | 70b)")
        return 1

    # 4. config
    cfg = config.load_config()
    cfg["provider"] = "ollama"
    ocfg = cfg.setdefault("ollama", {})
    ocfg["model"] = chosen
    ocfg.setdefault("timeout_sec", 300)
    config.save_config(cfg)
    print(f"[4/5] config saved: provider=ollama, ollama.model={chosen}")

    # 5. live test
    ok, detail = test_generation()
    print(f"[5/5] live brain test: {'PASS ✔' if ok else 'FAIL ✘'}  {detail}")
    if ok:
        print(f"\n✅ connected: mytool <-> Ollama <-> Hermes ({chosen})")
        print("   ab chalao:  mytool do-task 'disk usage report banao'")
        print("   heal test : mytool crash-test --demo")
        return 0
    return 1
