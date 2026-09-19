#!/usr/bin/env python
"""Houdini-side RPC listener. Run this inside hython or Houdini GUI.

    hython sidecar.py --serve

Or from Houdini Python Shell / 123.py:

    import sidecar
    sidecar.start_background()

Listens on 127.0.0.1 only. Requires HOUDINI_MCP_TOKEN (or --token).
hou calls in the GUI are marshalled onto the main thread.
"""

from __future__ import print_function

import argparse
import io
import os
import socket
import sys
import threading
import traceback

# Allow `hython sidecar.py` when this folder is not on PYTHONPATH.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from protocol import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_CODE_CHARS,
    MAX_MESSAGE_BYTES,
    dumps,
    error,
    loads,
    ok,
)


def _log(message):
    sys.stderr.write("[houdini-mcp-sidecar] %s\n" % message)
    sys.stderr.flush()


def _get_hou():
    try:
        import hou
        return hou
    except ImportError:
        return None


def _run_on_hou_thread(fn):
    hou = _get_hou()
    if hou is None:
        return fn()
    try:
        if hou.isUIAvailable():
            import hdefereval
            return hdefereval.executeInMainThreadWithResult(fn)
    except Exception:
        pass
    return fn()


def _session_info():
    hou = _get_hou()
    info = {
        "has_hou": hou is not None,
        "pid": os.getpid(),
        "executable": sys.executable,
    }
    if hou is None:
        return info
    info["houdini_version"] = hou.applicationVersionString()
    info["ui"] = bool(hou.isUIAvailable())
    try:
        info["hip"] = hou.hipFile.path()
    except Exception:
        info["hip"] = ""
    try:
        info["fps"] = hou.fps()
    except Exception:
        info["fps"] = None
    return info


def _exec_code(code, as_eval=False):
    if len(code) > MAX_CODE_CHARS:
        raise ValueError("code exceeds %d characters" % MAX_CODE_CHARS)
    hou = _get_hou()
    if hou is None:
        raise RuntimeError("hou is not available — start this sidecar with hython or Houdini")

    def work():
        namespace = {"hou": hou, "__name__": "__mcp__"}
        stdout = io.StringIO() if sys.version_info[0] >= 3 else io.BytesIO()
        old = sys.stdout
        sys.stdout = stdout
        try:
            if as_eval:
                value = eval(code, namespace, namespace)
            else:
                exec(code, namespace, namespace)
                value = namespace.get("_result")
        finally:
            sys.stdout = old
        text = stdout.getvalue()
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        try:
            import json
            json.dumps(value)
            result = value
        except Exception:
            result = repr(value)
        return {"result": result, "stdout": text}

    return _run_on_hou_thread(work)


def handle_request(message, expected_token):
    req_id = message.get("id")
    token = message.get("token")
    if not expected_token or token != expected_token:
        return error(req_id, "bad token", code="auth")
    op = message.get("op")
    payload = message.get("payload") or {}
    try:
        if op == "ping":
            return ok(req_id, {"pong": True})
        if op == "info":
            return ok(req_id, _session_info())
        if op == "exec":
            code = payload.get("code") or ""
            as_eval = bool(payload.get("eval"))
            data = _exec_code(code, as_eval=as_eval)
            return ok(req_id, data.get("result"), stdout=data.get("stdout") or "")
        if op == "shutdown":
            return ok(req_id, {"shutdown": True})
        return error(req_id, "unknown op %s" % op, code="bad_op")
    except Exception as exc:
        return error(req_id, "%s\n%s" % (exc, traceback.format_exc()), code="exec")


def _recv_line(conn):
    chunks = []
    total = 0
    while True:
        byte = conn.recv(1)
        if not byte:
            return None
        if byte in (b"\n", b"\r"):
            if chunks:
                break
            continue
        chunks.append(byte)
        total += 1
        if total > MAX_MESSAGE_BYTES:
            raise ValueError("message too large")
    return b"".join(chunks)


def _send(conn, message):
    data = (dumps(message) + "\n").encode("utf-8")
    conn.sendall(data)


class SidecarServer(object):
    def __init__(self, host, port, token):
        self.host = host
        self.port = int(port)
        self.token = token
        self._sock = None
        self._stop = threading.Event()

    def serve_forever(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        _log("listening on %s:%s pid=%s" % (self.host, self.port, os.getpid()))
        try:
            while not self._stop.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                if addr[0] not in ("127.0.0.1", "localhost", "::1"):
                    _log("rejected %s" % (addr,))
                    conn.close()
                    continue
                try:
                    self._handle_conn(conn)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        finally:
            sock.close()

    def _handle_conn(self, conn):
        conn.settimeout(120)
        while not self._stop.is_set():
            raw = _recv_line(conn)
            if raw is None:
                return
            try:
                message = loads(raw)
            except Exception as exc:
                _send(conn, error(None, "bad json: %s" % exc, code="protocol"))
                continue
            reply = handle_request(message, self.token)
            _send(conn, reply)
            if message.get("op") == "shutdown" and reply.get("ok"):
                self._stop.set()
                return

    def stop(self):
        self._stop.set()


_BACKGROUND = {"server": None, "thread": None}


def start_background(host=None, port=None, token=None):
    host = host or os.environ.get("HOUDINI_MCP_HOST") or DEFAULT_HOST
    port = int(port or os.environ.get("HOUDINI_MCP_PORT") or DEFAULT_PORT)
    token = token or os.environ.get("HOUDINI_MCP_TOKEN") or ""
    if not token:
        raise RuntimeError("HOUDINI_MCP_TOKEN is empty")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("sidecar must bind localhost, got %s" % host)
    server = SidecarServer(host, port, token)
    thread = threading.Thread(target=server.serve_forever, name="houdini-mcp-sidecar")
    thread.daemon = True
    thread.start()
    _BACKGROUND["server"] = server
    _BACKGROUND["thread"] = thread
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Houdini MCP sidecar")
    parser.add_argument("--serve", action="store_true", help="listen until shutdown")
    parser.add_argument("--host", default=os.environ.get("HOUDINI_MCP_HOST") or DEFAULT_HOST)
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("HOUDINI_MCP_PORT") or DEFAULT_PORT))
    parser.add_argument("--token", default=os.environ.get("HOUDINI_MCP_TOKEN") or "")
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        _log("refusing to bind %s" % args.host)
        return 2
    if not args.token:
        _log("HOUDINI_MCP_TOKEN / --token is required")
        return 2
    if not args.serve:
        start_background(args.host, args.port, args.token)
        return 0
    server = SidecarServer(args.host, args.port, args.token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
