# Houdini startup: start the Cursor MCP sidecar on localhost.
# This file is loaded because the package JSON points HOUDINI_PATH at
# houdini/mcp/houdini_package.

import os
import sys
import traceback


def _boot():
    root = os.environ.get("CURSOR_HOUDINI_MCP_ROOT")
    if not root:
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    mcp_dir = os.path.join(root, "houdini", "mcp")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    token = os.environ.get("HOUDINI_MCP_TOKEN")
    if not token:
        # Package JSON should have set this. Refuse to start an open listener.
        sys.stderr.write("[houdini-mcp] HOUDINI_MCP_TOKEN missing; sidecar not started\n")
        return
    import sidecar
    sidecar.start_background()
    sys.stderr.write("[houdini-mcp] sidecar on 127.0.0.1:%s\n" %
                     (os.environ.get("HOUDINI_MCP_PORT") or "18991"))


try:
    _boot()
except Exception:
    traceback.print_exc()
