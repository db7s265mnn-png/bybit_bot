import io
import json
import os
import sys
import unittest

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, HERE)

from framing import read_message, write_message  # noqa: E402


class FramingTests(unittest.TestCase):
    def test_roundtrip(self):
        buf = io.BytesIO()
        write_message(buf, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        buf.seek(0)
        message = read_message(buf)
        self.assertEqual(message["method"], "ping")
        self.assertEqual(message["id"], 1)

    def test_content_length_is_bytes(self):
        buf = io.BytesIO()
        payload = {"text": "скала"}
        write_message(buf, payload)
        raw = buf.getvalue()
        header, body = raw.split(b"\r\n\r\n", 1)
        length = int(header.split(b":")[1].strip())
        self.assertEqual(length, len(body))
        self.assertEqual(json.loads(body.decode("utf-8"))["text"], "скала")


if __name__ == "__main__":
    unittest.main()
