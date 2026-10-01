"""brain.py — JSON parsing, the three providers and the health check."""
from __future__ import annotations

import json

from my_ai_tool import brain, config

from .support import MockOllama, ToolTestCase


class TestJSONExtraction(ToolTestCase):
    def test_plain_json(self):
        self.assertEqual(brain.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(
            brain.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(brain.extract_json('```\n{"a": 2}\n```'), {"a": 2})

    def test_json_wrapped_in_prose(self):
        text = 'Sure! Here you go:\n{"commands": ["ls"]}\nHope that helps.'
        self.assertEqual(brain.extract_json(text), {"commands": ["ls"]})

    def test_garbage_raises_brainerror(self):
        for bad in ("", "no json at all", "{broken"):
            with self.assertRaises(brain.BrainError):
                brain.extract_json(bad)


class TestMockProvider(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.use_mock_brain()

    def test_planner_returns_commands(self):
        out = brain.chat([{"role": "system", "content": brain.PLANNER_SYSTEM},
                          {"role": "user", "content": "TASK: anything"}])
        self.assertTrue(out["commands"])

    def test_healer_patches_a_zero_division(self):
        out = brain.chat([
            {"role": "system", "content": brain.HEALER_SYSTEM},
            {"role": "user", "content":
                "CRASH_TYPE: ZeroDivisionError\nSUSPECT_FILE: examples/broken.py\n"
                "CURRENT_CODE:\n```python\ndef divide(a, b):\n    return 1 / b\n```"},
        ])
        self.assertEqual(out["file_to_fix"], "examples/broken.py")
        self.assertIn("float('inf')", out["full_corrected_code"])

    def test_healer_refuses_unknown_bugs(self):
        with self.assertRaises(brain.BrainError):
            brain.chat([
                {"role": "system", "content": brain.HEALER_SYSTEM},
                {"role": "user", "content":
                    "CRASH_TYPE: KeyError\nSUSPECT_FILE: a.py\n"
                    "CURRENT_CODE:\n```python\nx = {}\n```"},
            ])

    def test_vault_router_selects_minimal_files(self):
        out = brain.chat([
            {"role": "system", "content": "You are the vault router of mytool."},
            {"role": "user", "content":
                "TASK: report\nARCHIVE_INDEX:\n- scripts/report.py (10 bytes)\n"
                "- data/users.csv (5 bytes)\n- assets_readme.txt (3 bytes)"},
        ])
        self.assertEqual(sorted(out["needed_files"]),
                         ["data/users.csv", "scripts/report.py"])

    def test_ping(self):
        ok, detail = brain.ping()
        self.assertTrue(ok)
        self.assertIn("mock", detail)

    def test_unknown_provider(self):
        self.set_config(provider="banana")
        with self.assertRaises(brain.BrainError):
            brain.chat([{"role": "user", "content": "hi"}])


class TestOllamaProvider(ToolTestCase):
    """Exercises the real HTTP code path against the dev fake-Ollama."""

    def test_chat_and_ping_over_http(self):
        with MockOllama() as server:
            self.set_config(provider="ollama")
            cfg = config.load_config()
            cfg["ollama"]["url"] = server.url
            cfg["ollama"]["model"] = "hermes3:3b"
            config.save_config(cfg)

            ok, detail = brain.ping()
            self.assertTrue(ok, detail)
            self.assertIn("hermes3:3b", detail)

            out = brain.chat([
                {"role": "system", "content": brain.PLANNER_SYSTEM},
                {"role": "user", "content": "TASK: make a demo file"}])
            self.assertTrue(out["commands"])

    def test_model_autodetect_when_unset(self):
        with MockOllama() as server:
            self.set_config(provider="ollama")
            cfg = config.load_config()
            cfg["ollama"]["url"] = server.url
            cfg["ollama"]["model"] = ""          # empty -> auto-detect
            config.save_config(cfg)
            self.assertEqual(brain._ollama_first_model(server.url), "hermes3:3b")
            self.assertTrue(brain.chat([{"role": "user", "content": "ok?"}]))

    def test_unreachable_server_reports_a_helpful_error(self):
        self.set_config(provider="ollama")
        cfg = config.load_config()
        cfg["ollama"]["url"] = "http://127.0.0.1:1"   # nothing listens here
        cfg["ollama"]["model"] = ""
        config.save_config(cfg)
        ok, detail = brain.ping()
        self.assertFalse(ok)
        self.assertIn("ollama", detail.lower())


class TestOpenAIProvider(ToolTestCase):
    def test_missing_api_key_is_reported(self):
        self.set_config(provider="openai")
        import os
        os.environ.pop("OPENAI_API_KEY", None)
        ok, detail = brain.ping()
        self.assertFalse(ok)
        self.assertIn("NOT set", detail)
        with self.assertRaises(brain.BrainError):
            brain.chat([{"role": "user", "content": json.dumps({"x": 1})}])
