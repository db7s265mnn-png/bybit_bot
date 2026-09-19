"""JSON-line protocol between the MCP server and the Houdini sidecar.

The sidecar binds 127.0.0.1 only. Every request carries a shared token so a
random local process cannot drive Houdini. This is not a general shell.
"""

from __future__ import print_function

import json

PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18991
MAX_CODE_CHARS = 200000
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

OPS = ("ping", "info", "exec", "shutdown")


def dumps(message):
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def loads(line):
    if not line:
        raise ValueError("empty protocol line")
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("protocol message must be an object")
    return message


def request(op, token, payload=None, req_id=None):
    if op not in OPS:
        raise ValueError("unknown op: %s" % op)
    message = {
        "v": PROTOCOL_VERSION,
        "op": op,
        "token": token,
    }
    if req_id is not None:
        message["id"] = req_id
    if payload:
        message["payload"] = payload
    return message


def ok(req_id, result=None, stdout=""):
    return {
        "v": PROTOCOL_VERSION,
        "id": req_id,
        "ok": True,
        "stdout": stdout or "",
        "result": result,
    }


def error(req_id, message, code="error"):
    return {
        "v": PROTOCOL_VERSION,
        "id": req_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }
