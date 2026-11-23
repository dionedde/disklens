"""AI assistant against a fake local server (no real provider is called, no cost).

Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from disklens import ai  # noqa: E402

SEEN = []
REPLY = {"summary": "Dá para liberar 5 GB.", "items": [
    {"id": "safe:1", "verdict": "apagar", "confidence": 0.95, "reason": "cache"},
    {"id": "prog:9", "verdict": "desinstalar", "confidence": 0.7, "reason": "jogo parado"},
    {"id": "inventado:C:\\Windows", "verdict": "apagar", "confidence": 1, "reason": "x"},   # must be dropped
]}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        SEEN.append(("GET", self.path, dict(self.headers)))
        self._send({"data": [{"id": "modelo-b"}, {"id": "modelo-a"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        text = "```json\n" + json.dumps(REPLY) + "\n```"
        if self.path.endswith("/messages"):
            self._send({"content": [{"type": "text", "text": text}]})
        elif self.path.endswith("/chat/completions"):
            if body["model"] == "erro":
                return self._send({"error": {"message": "modelo não existe"}}, 404)
            self._send({"choices": [{"message": {"content": text}}]})


class AITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp(prefix="disklens_ai_")
        os.environ["DISKLENS_DATA"] = cls.tmp
        cls.cfg = {}
        ai.load_config = lambda: dict(cls.cfg)          # never touch the real config.json
        ai.save_config = lambda c: (cls.cfg.clear(), cls.cfg.update(c))
        ai.PROVIDERS["anthropic_test"] = dict(ai.PROVIDERS["anthropic"], base=cls.base)
        ai.candidates.build = lambda vol: {
            "available": True, "snapshot": {"total": 2e11, "used": 1.9e11, "free": 1e10},
            "safe": [{"id": "safe:1", "path": r"C:\Users\x\AppData\Local\Temp", "size": 5e9, "label": "Temp"}],
            "tools": [], "review": [], "unknown": [],
            "programs": [{"id": "prog:9", "name": "Jogo", "publisher": "", "drive": "C:", "size": 1e9,
                          "installed": None, "last_change": None, "dependency": False}]}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.pop("DISKLENS_DATA", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        SEEN.clear()

    def test_dpapi_roundtrip(self):
        enc = ai.protect("sk-segredo-123")
        self.assertNotIn("segredo", enc)
        self.assertEqual(ai.unprotect(enc), "sk-segredo-123")

    def test_extract_json(self):
        self.assertEqual(ai.extract_json('Claro!\n```json\n{"a": 1}\n```')["a"], 1)
        with self.assertRaises(ai.AIError):
            ai.extract_json("sem json aqui")

    def test_openai_compatible_flow(self):
        ai.save_settings(provider="custom", base_url=self.base, api_key="sk-teste", model="modelo-a", consented=True)
        self.assertNotIn("sk-teste", json.dumps(self.cfg))            # stored encrypted
        self.assertEqual(ai.list_models(), ["modelo-a", "modelo-b"])
        r = ai.analyze("C:")
        ids = [i["id"] for i in r["items"]]
        self.assertEqual(ids, ["safe:1", "prog:9"])                    # invented id dropped
        post = [s for s in SEEN if s[0] == "POST"][0]
        self.assertEqual(post[1], "/v1/chat/completions")
        self.assertEqual(post[2]["authorization"], "Bearer sk-teste")
        self.assertEqual(post[3]["messages"][0]["role"], "system")
        self.assertTrue((Path(self.tmp) / "ai_last.json").exists())

    def test_anthropic_flow(self):
        ai.save_settings(provider="anthropic_test", api_key="ant-key", model="claude-x")
        r = ai.analyze("C:")
        self.assertEqual(len(r["items"]), 2)
        post = [s for s in SEEN if s[0] == "POST"][0]
        self.assertEqual(post[1], "/v1/messages")
        self.assertEqual(post[2]["x-api-key"], "ant-key")
        self.assertEqual(post[2]["anthropic-version"], "2023-06-01")
        self.assertIn("system", post[3])

    def test_http_error_is_readable(self):
        ai.save_settings(provider="custom", base_url=self.base, model="erro")
        with self.assertRaises(ai.AIError) as cm:
            ai.chat("s", "u")
        self.assertIn("modelo não existe", str(cm.exception))

    def test_missing_key(self):
        ai.save_settings(provider="openai", model="x")
        with self.assertRaises(ai.AIError):
            ai.chat("s", "u")


if __name__ == "__main__":
    unittest.main()
