"""LSP-style Content-Length framing used by MCP stdio transport."""

from __future__ import print_function

import json
import sys


def write_message(stream, message):
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    header = ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def _read_headers(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if isinstance(line, bytes):
            if line in (b"\r\n", b"\n"):
                break
            text = line.decode("ascii", errors="replace")
        else:
            if line in ("\r\n", "\n"):
                break
            text = line
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def read_message(stream):
    headers = _read_headers(stream)
    if headers is None:
        return None
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = stream.read(length)
    if isinstance(body, bytes):
        if len(body) < length:
            return None
        return json.loads(body.decode("utf-8"))
    # Text mode: Content-Length is bytes, this is a last-resort fallback.
    return json.loads(body)


def wrap_stdio():
    """Binary stdin/stdout so Content-Length counts bytes, not characters."""
    stdin = sys.stdin
    stdout = sys.stdout
    if hasattr(stdin, "buffer"):
        stdin = stdin.buffer
    if hasattr(stdout, "buffer"):
        stdout = stdout.buffer
    return stdin, stdout
