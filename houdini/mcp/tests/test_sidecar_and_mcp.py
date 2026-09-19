import os
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, HERE)

from client import SidecarClient, SidecarError  # noqa: E402
from framing import read_message, write_message  # noqa: E402
from protocol import DEFAULT_HOST  # noqa: E402
from sidecar import SidecarServer, handle_request  # noqa: E402


class ProtocolAuthTests(unittest.TestCase):
    def test_bad_token(self):
        reply = handle_request({"op": "ping", "token": "nope", "id": "1"}, "secret")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "auth")

    def test_ping(self):
        reply = handle_request({"op": "ping", "token": "secret", "id": "1"}, "secret")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["pong"], True)


class SidecarLoopTests(unittest.TestCase):
    def test_python_sidecar_without_houdini(self):
        port = 18997
        token = "test-token-sidecar"
        server = SidecarServer(DEFAULT_HOST, port, token)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        time.sleep(0.15)
        try:
            with SidecarClient(DEFAULT_HOST, port, token, timeout=3) as client:
                self.assertTrue(client.ping()["ok"])
                info = client.info()["result"]
                self.assertIn("has_hou", info)
                self.assertFalse(info["has_hou"])
                with self.assertRaises(SidecarError):
                    client.exec_python("1+1", as_eval=True)
        finally:
            try:
                with SidecarClient(DEFAULT_HOST, port, token, timeout=2) as client:
                    client.shutdown()
            except Exception:
                server.stop()
            thread.join(timeout=2)


class McpHandshakeTests(unittest.TestCase):
    def test_initialize_and_tools(self):
        env = os.environ.copy()
        env["HOUDINI_MCP_HOST"] = "127.0.0.1"
        env["HOUDINI_MCP_PORT"] = "18991"
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            write_message(proc.stdin, {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            })
            reply = read_message(proc.stdout)
            self.assertEqual(reply["id"], 1)
            self.assertEqual(reply["result"]["serverInfo"]["name"], "houdini")
            self.assertIn("tools", reply["result"]["capabilities"])

            write_message(proc.stdin, {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            })
            listed = read_message(proc.stdout)
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertIn("houdini_exec", names)
            self.assertIn("houdini_build_rock", names)
            self.assertIn("houdini_start", names)

            write_message(proc.stdin, {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "houdini_status", "arguments": {}},
            })
            status = read_message(proc.stdout)
            text = status["result"]["content"][0]["text"]
            self.assertIn("sidecar_listening", text)
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    unittest.main()
