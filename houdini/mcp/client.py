"""TCP client for the localhost Houdini sidecar."""

from __future__ import print_function

import socket
import uuid

from protocol import DEFAULT_HOST, DEFAULT_PORT, dumps, loads, request


class SidecarError(RuntimeError):
    def __init__(self, message, code="error"):
        RuntimeError.__init__(self, message)
        self.code = code


class SidecarClient(object):
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, token="", timeout=60):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise SidecarError("refusing to connect to %s" % host, code="auth")
        self.host = host
        self.port = int(port)
        self.token = token
        self.timeout = timeout
        self._sock = None

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def _recv_line(self):
        chunks = []
        while True:
            byte = self._sock.recv(1)
            if not byte:
                raise SidecarError("sidecar closed the connection", code="closed")
            if byte in (b"\n", b"\r"):
                if chunks:
                    break
                continue
            chunks.append(byte)
        return b"".join(chunks)

    def call(self, op, payload=None, timeout=None):
        if self._sock is None:
            self.connect()
        if timeout is not None:
            self._sock.settimeout(timeout)
        req_id = uuid.uuid4().hex
        raw = (dumps(request(op, self.token, payload=payload, req_id=req_id)) + "\n")
        self._sock.sendall(raw.encode("utf-8"))
        reply = loads(self._recv_line())
        if not reply.get("ok"):
            err = reply.get("error") or {}
            raise SidecarError(err.get("message") or "sidecar error",
                               code=err.get("code") or "error")
        return reply

    def ping(self):
        return self.call("ping")

    def info(self):
        return self.call("info")

    def exec_python(self, code, as_eval=False, timeout=None):
        return self.call("exec", {"code": code, "eval": bool(as_eval)}, timeout=timeout)

    def shutdown(self):
        try:
            return self.call("shutdown", timeout=5)
        finally:
            self.close()


def port_open(host, port, timeout=0.25):
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False
