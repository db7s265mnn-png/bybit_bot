"""Start / stop a Houdini sidecar process and keep the shared token."""

from __future__ import print_function

import os
import stat
import subprocess
import sys
import time

from client import SidecarClient, SidecarError, port_open
from discover import discover
from protocol import DEFAULT_HOST, DEFAULT_PORT

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".cursor_houdini_mcp_token")


def repo_root():
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def load_or_create_token():
    env = os.environ.get("HOUDINI_MCP_TOKEN")
    if env:
        return env
    if os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as handle:
            token = handle.read().strip()
        if token:
            return token
    token = os.urandom(16).hex()
    save_token(token)
    return token


def save_token(token):
    folder = os.path.dirname(TOKEN_FILE)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(TOKEN_FILE, "w") as handle:
        handle.write(token)
    try:
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def host_port():
    host = os.environ.get("HOUDINI_MCP_HOST") or DEFAULT_HOST
    port = int(os.environ.get("HOUDINI_MCP_PORT") or DEFAULT_PORT)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SidecarError("HOUDINI_MCP_HOST must be localhost", code="auth")
    return host, port


def connect(timeout=8):
    host, port = host_port()
    client = SidecarClient(host, port, token=load_or_create_token(), timeout=timeout)
    client.connect()
    return client


def status():
    info = discover()
    host, port = host_port()
    listening = port_open(host, port)
    session = None
    if listening:
        try:
            with connect(timeout=4) as client:
                session = client.info().get("result")
        except Exception as exc:
            session = {"error": str(exc)}
    info.update({
        "sidecar_host": host,
        "sidecar_port": port,
        "sidecar_listening": listening,
        "session": session,
        "token_file": TOKEN_FILE,
        "repo": repo_root(),
    })
    return info


def start(mode="hython", wait_sec=90):
    current = status()
    if current.get("sidecar_listening") and current.get("session") and not current["session"].get("error"):
        return {"already_running": True, "session": current["session"]}

    found = discover()
    if mode == "houdini":
        binary = found.get("houdini")
    else:
        binary = found.get("hython")
        mode = "hython"
    if not binary:
        raise SidecarError(
            "Houdini/hython not found. Set HFS or install SideFX Houdini.",
            code="no_houdini",
        )

    host, port = host_port()
    token = load_or_create_token()
    sidecar = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidecar.py")
    env = os.environ.copy()
    env["HOUDINI_MCP_TOKEN"] = token
    env["HOUDINI_MCP_HOST"] = host
    env["HOUDINI_MCP_PORT"] = str(port)
    env["CURSOR_HOUDINI_MCP_ROOT"] = repo_root()
    if found.get("hfs"):
        env.setdefault("HFS", found["hfs"])

    log_path = os.path.join(os.path.expanduser("~"), ".cursor_houdini_mcp_sidecar.log")
    log = open(log_path, "ab")
    if mode == "hython":
        cmd = [binary, sidecar, "--serve", "--host", host, "--port", str(port), "--token", token]
    else:
        package = os.path.join(os.path.dirname(os.path.abspath(__file__)), "houdini_package")
        # `&` means "append the default HOUDINI_PATH" (SideFX convention).
        env["HOUDINI_PATH"] = package.replace("\\", "/") + ";&"
        cmd = [binary]

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=repo_root(),
    )
    deadline = time.time() + wait_sec
    last_error = "sidecar did not open %s:%s" % (host, port)
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SidecarError(
                "Houdini exited early (code %s). See %s" % (proc.returncode, log_path),
                code="exited",
            )
        if port_open(host, port):
            try:
                with connect(timeout=5) as client:
                    session = client.info().get("result")
                return {
                    "already_running": False,
                    "pid": proc.pid,
                    "mode": mode,
                    "cmd": cmd[:2],
                    "log": log_path,
                    "session": session,
                }
            except Exception as exc:
                last_error = str(exc)
        time.sleep(0.4)
    raise SidecarError(last_error + " (log: %s)" % log_path, code="timeout")


def stop():
    host, port = host_port()
    if not port_open(host, port):
        return {"stopped": False, "reason": "not listening"}
    try:
        with connect(timeout=5) as client:
            client.shutdown()
    except Exception as exc:
        return {"stopped": False, "reason": str(exc)}
    time.sleep(0.3)
    return {"stopped": True, "listening": port_open(host, port)}


def run_python(code, as_eval=False, timeout=60, autostart=True):
    host, port = host_port()
    if not port_open(host, port):
        if not autostart:
            raise SidecarError("sidecar is not running", code="down")
        start(mode="hython")
    with connect(timeout=timeout) as client:
        reply = client.exec_python(code, as_eval=as_eval, timeout=timeout)
    return {
        "result": reply.get("result"),
        "stdout": reply.get("stdout") or "",
    }
