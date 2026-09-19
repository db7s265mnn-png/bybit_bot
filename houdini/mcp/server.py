#!/usr/bin/env python3
"""Cursor MCP server: drive a local SideFX Houdini sidecar.

Stdio / Content-Length only. Logs go to stderr. Does not expose a shell.
"""

from __future__ import print_function

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from client import SidecarError  # noqa: E402
from framing import read_message, wrap_stdio, write_message  # noqa: E402
from session import run_python, start, status, stop  # noqa: E402
from tools_impl import (  # noqa: E402
    build_rock,
    cook_node,
    export_geo,
    save_hip,
    scene_tree,
    screenshot,
    set_parm,
)

SERVER_NAME = "houdini"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"


def _text(payload):
    if isinstance(payload, str):
        text = payload
    else:
        try:
            import json
            text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        except Exception:
            text = repr(payload)
    return [{"type": "text", "text": text}]


TOOLS = {
    "houdini_status": {
        "description": (
            "Find SideFX Houdini/hython on this machine and report whether "
            "the localhost sidecar is running."
        ),
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: status(),
    },
    "houdini_start": {
        "description": (
            "Start a local Houdini sidecar on 127.0.0.1. mode=hython is "
            "headless; mode=houdini opens the GUI so viewport screenshots work."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["hython", "houdini"],
                    "default": "hython",
                }
            },
        },
        "handler": lambda args: start(mode=args.get("mode") or "hython"),
    },
    "houdini_stop": {
        "description": "Ask the sidecar to shut down.",
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: stop(),
    },
    "houdini_exec": {
        "description": (
            "Run Python inside the live Houdini session (hou is already imported). "
            "Assign _result to return a value. Not a system shell."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "eval": {"type": "boolean", "default": False},
                "timeout_sec": {"type": "number", "default": 60},
            },
            "required": ["code"],
        },
        "handler": lambda args: run_python(
            args.get("code") or "",
            as_eval=bool(args.get("eval")),
            timeout=float(args.get("timeout_sec") or 60),
        ),
    },
    "houdini_nodes": {
        "description": "List Houdini nodes under a path.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/obj"},
                "depth": {"type": "integer", "default": 2},
            },
        },
        "handler": lambda args: scene_tree(
            args.get("path") or "/obj",
            int(args.get("depth") or 2),
        ),
    },
    "houdini_cook": {
        "description": "Force-cook a node and return point/prim counts when it has geometry.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": lambda args: cook_node(args["path"]),
    },
    "houdini_build_rock": {
        "description": (
            "Build the procedural limestone VDB→polygon network from this repo. "
            "shape 0 = cliff wall, shape 1 = pinnacle."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "shape": {"type": "integer", "enum": [0, 1], "default": 0},
                "seed": {"type": "integer", "default": 1},
                "cook": {"type": "boolean", "default": True},
                "geo_name": {"type": "string", "default": "procedural_rock"},
            },
        },
        "handler": lambda args: build_rock(
            shape=int(args.get("shape") or 0),
            seed=int(args.get("seed") or 1),
            cook=args.get("cook", True),
            geo_name=args.get("geo_name") or "procedural_rock",
        ),
    },
    "houdini_save_hip": {
        "description": "Save the current hip file.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": lambda args: save_hip(args["path"]),
    },
    "houdini_export_geo": {
        "description": "Export a SOP's geometry to bgeo/obj/abc (whatever Houdini accepts).",
        "schema": {
            "type": "object",
            "properties": {
                "node": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["node", "path"],
        },
        "handler": lambda args: export_geo(args["node"], args["path"]),
    },
    "houdini_set_parm": {
        "description": "Set a node parameter (float/int/string or float triple).",
        "schema": {
            "type": "object",
            "properties": {
                "node": {"type": "string"},
                "parm": {"type": "string"},
                "value": {},
            },
            "required": ["node", "parm", "value"],
        },
        "handler": lambda args: set_parm(args["node"], args["parm"], args["value"]),
    },
    "houdini_screenshot": {
        "description": "Save the current Scene Viewer image. Requires GUI Houdini (houdini_start mode=houdini).",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": lambda args: screenshot(args["path"]),
    },
    "houdini_install_autostart": {
        "description": (
            "Write a Houdini package so the sidecar starts whenever the user "
            "opens Houdini (123.py)."
        ),
        "schema": {"type": "object", "properties": {}},
        "handler": lambda args: _install_autostart(),
    },
}


def _install_autostart():
    from install_package import install
    return install()


def _tool_list():
    tools = []
    for name, spec in TOOLS.items():
        tools.append({
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["schema"],
        })
    return tools


def _call_tool(name, arguments):
    spec = TOOLS.get(name)
    if spec is None:
        raise SidecarError("unknown tool %s" % name, code="unknown_tool")
    return spec["handler"](arguments or {})


def _handle(message):
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    req_id = message.get("id")
    if method is None:
        return None
    # Notifications have no id.
    if req_id is None and method != "ping":
        return None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_list()}
        elif method == "tools/call":
            params = message.get("params") or {}
            data = _call_tool(params.get("name"), params.get("arguments") or {})
            result = {"content": _text(data)}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "Method not found: %s" % method},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except Exception as exc:
        err = {
            "content": _text("%s\n%s" % (exc, traceback.format_exc())),
            "isError": True,
        }
        if isinstance(exc, SidecarError) or method == "tools/call":
            return {"jsonrpc": "2.0", "id": req_id, "result": err}
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": str(exc)},
        }


def serve(stdin=None, stdout=None):
    if stdin is None or stdout is None:
        stdin, stdout = wrap_stdio()
    while True:
        message = read_message(stdin)
        if message is None:
            return
        reply = _handle(message)
        if reply is not None and message.get("id") is not None:
            write_message(stdout, reply)


if __name__ == "__main__":
    try:
        serve()
    except KeyboardInterrupt:
        sys.exit(0)
