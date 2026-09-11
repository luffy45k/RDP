"""Dev-only fake Ollama server (sandbox testing).

Real Ollama/registry sandbox ke network se blocked hai, isliye yeh chhota
server Ollama ki API imitate karta hai (/api/tags + /api/chat) taaki
`mytool setup-ollama` aur poora brain pipeline end-to-end test ho sake.
Model naam hermes3:3b dikhata hai jaisa real setup mein hoga.

Run:  python3 dev/mock_ollama_server.py
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = [{"name": "hermes3:3b", "model": "hermes3:3b",
           "size": 2_000_000_000, "digest": "mock"}]


def chat_reply(messages: list) -> dict:
    system = messages[0].get("content", "") if messages else ""
    text = "\n".join(str(m.get("content", "")) for m in messages)

    # healer prompt
    if "self-repair agent" in system:
        m = re.search(r"SUSPECT_FILE: (.+)", text)
        path = m.group(1).strip() if m else "unknown.py"
        cm = re.search(r"```python\n(.*?)```", text, re.S)
        code = cm.group(1) if cm else ""
        if "ZeroDivisionError" in text:
            fixed = code.replace("1 / b", "1 / b if b else float('inf')")
            if fixed == code:
                fixed = code.replace("1/b", "1 / b if b else float('inf')")
            return {"analysis": "hermes3(mock): guard division by zero",
                    "file_to_fix": path,
                    "full_corrected_code": fixed}
        return {"analysis": "hermes3(mock): no known demo bug found",
                "file_to_fix": path, "full_corrected_code": code}

    # follow-up / planner prompt
    if "TASK" in text or "INCOMPLETE" in text:
        return {"explanation": "hermes3(mock): demo file bana deta hoon",
                "commands": ["echo 'namaste from hermes3:3b' > ~/hermes-demo.txt",
                             "cat ~/hermes-demo.txt"]}

    # tiny ping
    if "ok" in text.lower():
        return {"ok": True, "note": "hermes3:3b (mock) alive"}

    return {"explanation": "hermes3(mock): nothing to do", "commands": []}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": MODELS})
        else:
            self._send({"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        if self.path.startswith("/api/chat"):
            content = json.dumps(chat_reply(payload.get("messages", [])))
            self._send({"model": payload.get("model", ""),
                        "message": {"role": "assistant", "content": content}})
        else:
            self._send({"ok": True})

    def log_message(self, *args):  # quiet
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "11434"))
    print(f"mock ollama (hermes3:3b) listening on 0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
