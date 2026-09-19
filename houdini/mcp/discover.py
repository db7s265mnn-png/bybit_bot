"""Locate SideFX Houdini / hython on Windows, macOS, and Linux."""

from __future__ import print_function

import glob
import os
import sys


def _is_exe(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _windows_program_files():
    paths = []
    for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(key)
        if value and value not in paths:
            paths.append(value)
    if not paths:
        paths.append(r"C:\Program Files")
    return paths


def _candidate_bins():
    bins = []
    hfs = os.environ.get("HFS") or os.environ.get("HFS_20_5") or ""
    if hfs:
        bins.append(os.path.join(hfs, "bin"))

    if sys.platform.startswith("win"):
        for root in _windows_program_files():
            bins.extend(sorted(glob.glob(os.path.join(
                root, "Side Effects Software", "Houdini *", "bin"
            ))))
            bins.extend(sorted(glob.glob(os.path.join(
                root, "SideFX", "Houdini *", "bin"
            ))))
    elif sys.platform == "darwin":
        bins.extend(sorted(glob.glob(
            "/Applications/Houdini/Houdini*/Frameworks/Houdini.framework/Versions/Current/Resources/bin"
        )))
        bins.extend(sorted(glob.glob("/Applications/Houdini*/Houdini*.app/Contents/MacOS")))
    else:
        bins.extend(sorted(glob.glob("/opt/hfs*/bin")))
        bins.extend(sorted(glob.glob(os.path.expanduser("~/houdini/hfs*/bin"))))

    which_hython = _which("hython.exe" if sys.platform.startswith("win") else "hython")
    if which_hython:
        bins.insert(0, os.path.dirname(which_hython))
    return bins


def _which(name):
    path = os.environ.get("PATH", "")
    for folder in path.split(os.pathsep):
        candidate = os.path.join(folder, name)
        if _is_exe(candidate):
            return candidate
    return None


def _pick(folder, names):
    for name in names:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def discover():
    """Return a dict describing the best Houdini install we can see."""
    hython_names = ("hython.exe", "hython")
    gui_names = (
        "houdinifx.exe", "houdinicore.exe", "houdini.exe",
        "houdinifx", "houdinicore", "houdini",
    )
    found = []
    seen = set()
    for folder in _candidate_bins():
        folder = os.path.abspath(folder)
        if folder in seen:
            continue
        seen.add(folder)
        hython = _pick(folder, hython_names)
        gui = _pick(folder, gui_names)
        if not hython and not gui:
            continue
        hfs = os.path.dirname(folder)
        found.append({
            "hfs": hfs,
            "bin": folder,
            "hython": hython,
            "houdini": gui,
            "version": os.path.basename(hfs).replace("Houdini", "").strip(),
        })

    # Newest-looking path last in glob; prefer the last entry.
    best = found[-1] if found else None
    return {
        "platform": sys.platform,
        "hfs_env": os.environ.get("HFS") or "",
        "installs": found,
        "hython": best["hython"] if best else None,
        "houdini": best["houdini"] if best else None,
        "hfs": best["hfs"] if best else None,
    }
