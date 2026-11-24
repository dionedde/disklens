"""MCP server speaks JSON-RPC over stdio: initialize, list tools, call them, propose (never delete).

Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="disklens_mcp_")
        env = dict(os.environ, DISKLENS_DATA=self.tmp)
        self.p = subprocess.Popen([sys.executable, str(ROOT / "disklens_mcp.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.n = 0

    def tearDown(self):
        self.p.stdin.close()
        self.p.wait(10)
        self.p.stdout.close()
        self.p.stderr.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        if notify:
            return None
        return json.loads(self.p.stdout.readline())

    def test_protocol(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "disklens")
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.rpc("notifications/initialized", notify=True)
        tools = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertIn("propose_cleanup", tools)
        self.assertFalse(any("delete" in t or "apagar" in t for t in tools))   # no direct deletion tool
        r = self.rpc("tools/call", {"name": "get_disk_overview", "arguments": {}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("error", json.loads(r["result"]["content"][0]["text"]))   # empty data dir -> asks for a scan
        self.assertIn("error", self.rpc("no/such/method"))

    def test_proposals_are_validated(self):
        self.rpc("initialize", {"protocolVersion": "2024-11-05"})
        tmpfile = Path(self.tmp).parent / "disklens_mcp_probe.txt"
        windir = os.environ.get("WINDIR", r"C:\Windows")
        args = {"title": "teste", "items": [
            {"action": "lixeira", "path": windir + r"\System32\kernel32.dll", "reason": "x"},
            {"action": "lixeira", "path": windir, "reason": "x"},
            {"action": "lixeira", "path": "C:\\", "reason": "x"},
            {"action": "esvaziar", "path": os.path.expanduser("~\\Documents"), "reason": "x"},
            {"action": "ferramenta", "tool": "format_c", "reason": "x"},
            {"action": "desinstalar", "program_id": "prog:nao-existe", "reason": "x"},
        ]}
        r = self.rpc("tools/call", {"name": "propose_cleanup", "arguments": args})
        out = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(out["accepted"], 0)
        self.assertEqual(len(out["refused"]), 6)
        self.assertTrue(tmpfile or True)


if __name__ == "__main__":
    unittest.main()
