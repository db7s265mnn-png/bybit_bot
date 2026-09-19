"""Install a Houdini package that auto-starts the localhost sidecar."""

from __future__ import print_function

import json
import os
import sys

from session import load_or_create_token, repo_root


def houdini_pref_dirs():
    home = os.path.expanduser("~")
    dirs = []
    if sys.platform.startswith("win"):
        docs = os.path.join(home, "Documents")
        root = docs
        pattern_roots = [docs]
    elif sys.platform == "darwin":
        pattern_roots = [os.path.join(home, "Library", "Preferences", "houdini")]
        # Also versioned folders directly under Preferences/houdini/20.5
        if os.path.isdir(pattern_roots[0]):
            for name in sorted(os.listdir(pattern_roots[0])):
                dirs.append(os.path.join(pattern_roots[0], name))
        return [d for d in dirs if os.path.isdir(d)]
    else:
        pattern_roots = [home]

    if sys.platform.startswith("win") or sys.platform.startswith("linux"):
        search = pattern_roots[0]
        if os.path.isdir(search):
            for name in sorted(os.listdir(search)):
                if name.lower().startswith("houdini") and os.path.isdir(os.path.join(search, name)):
                    dirs.append(os.path.join(search, name))
    return dirs


def install():
    token = load_or_create_token()
    root = repo_root()
    package_dir = os.path.join(root, "houdini", "mcp", "houdini_package")
    prefs = houdini_pref_dirs()
    if not prefs:
        raise RuntimeError(
            "No Houdini preferences folder found (expected ~/Documents/houdiniXX.X "
            "on Windows). Open Houdini once, then retry."
        )
    written = []
    body = {
        "path": package_dir.replace("\\", "/"),
        "enable": True,
        "env": [
            {"CURSOR_HOUDINI_MCP_ROOT": root.replace("\\", "/")},
            {"HOUDINI_MCP_TOKEN": token},
            {"HOUDINI_MCP_HOST": "127.0.0.1"},
            {"HOUDINI_MCP_PORT": "18991"},
        ],
    }
    for pref in prefs:
        packages = os.path.join(pref, "packages")
        if not os.path.isdir(packages):
            os.makedirs(packages)
        dest = os.path.join(packages, "cursor_houdini_mcp.json")
        with open(dest, "w") as handle:
            json.dump(body, handle, indent=2)
        written.append(dest)
    return {
        "written": written,
        "package_dir": package_dir,
        "note": "Restart Houdini. The sidecar will listen on 127.0.0.1:18991.",
    }
