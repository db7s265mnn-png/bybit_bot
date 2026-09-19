"""Houdini-side Python snippets used by MCP tools.

These strings are executed *inside* hython / Houdini via the sidecar, not
on the MCP host. Keep them free of MCP imports.
"""

from __future__ import print_function

import json
import os

from session import repo_root, run_python


def _py_literal(value):
    return json.dumps(value)


def scene_tree(path="/obj", depth=2):
    code = r'''
path = %s
depth = %s
root = hou.node(path)
if root is None:
    _result = {"error": "no node %%s" %% path}
else:
    def walk(node, remaining):
        item = {
            "name": node.name(),
            "type": node.type().name(),
            "path": node.path(),
        }
        if remaining > 0:
            item["children"] = [walk(child, remaining - 1) for child in node.children()]
        return item
    _result = walk(root, depth)
''' % (_py_literal(path), int(depth))
    return run_python(code)


def cook_node(path):
    code = r'''
path = %s
node = hou.node(path)
if node is None:
    raise RuntimeError("no node %%s" %% path)
node.cook(force=True)
geo = getattr(node, "geometry", lambda: None)()
info = {"path": node.path(), "type": node.type().name(), "cooked": True}
if geo is not None:
    info["points"] = geo.intrinsicValue("pointcount")
    info["prims"] = geo.intrinsicValue("primitivecount")
_result = info
''' % _py_literal(path)
    return run_python(code, timeout=300)


def save_hip(path):
    path = os.path.abspath(path)
    code = r'''
path = %s
hou.hipFile.save(file_name=path)
_result = {"hip": hou.hipFile.path()}
''' % _py_literal(path)
    return run_python(code, timeout=120)


def export_geo(node_path, file_path):
    file_path = os.path.abspath(file_path)
    code = r'''
node_path = %s
file_path = %s
node = hou.node(node_path)
if node is None:
    raise RuntimeError("no node %%s" %% node_path)
geo = node.geometry()
if geo is None:
    raise RuntimeError("node %%s has no geometry" %% node_path)
geo.saveToFile(file_path)
_result = {"path": file_path, "points": geo.intrinsicValue("pointcount"),
           "prims": geo.intrinsicValue("primitivecount")}
''' % (_py_literal(node_path), _py_literal(file_path))
    return run_python(code, timeout=180)


def set_parm(node_path, parm_name, value):
    code = r'''
node = hou.node(%s)
if node is None:
    raise RuntimeError("no node %%s" %% %s)
value = %s
parm = node.parm(%s)
if parm is not None:
    parm.set(value)
    _result = {"path": node.path(), "parm": parm.name(), "value": parm.eval()}
else:
    ptuple = node.parmTuple(%s)
    if ptuple is None:
        raise RuntimeError("no parm %%s on %%s" %% (%s, node.path()))
    ptuple.set(value if isinstance(value, (list, tuple)) else [value])
    _result = {"path": node.path(), "parm": ptuple.name(), "value": list(ptuple.eval())}
''' % (
        _py_literal(node_path),
        _py_literal(node_path),
        _py_literal(value),
        _py_literal(parm_name),
        _py_literal(parm_name),
        _py_literal(parm_name),
    )
    return run_python(code)


def build_rock(shape=0, seed=1, cook=True, geo_name="procedural_rock"):
    builder = os.path.join(repo_root(), "houdini", "procedural_rock", "python",
                           "build_rock_network.py")
    if not os.path.isfile(builder):
        raise RuntimeError("missing %s" % builder)
    out = "/obj/%s/OUT_ROCK" % geo_name
    code = r'''
builder = %s
shape = %d
seed = %d
geo_name = %s
do_cook = %s
ns = {}
exec(compile(open(builder, "r").read(), builder, "exec"), ns, ns)
geo = ns["build"](geo_name=geo_name, shape=shape)
for name in ("vol_01_base_mass", "vol_02_macro_displace",
             "vol_03_strata_cracks", "vol_04_weathering"):
    node = geo.node(name)
    if node is None:
        continue
    if node.parm("seed"):
        node.parm("seed").set(seed)
out = geo.node("OUT_ROCK")
info = {"geo": geo.path(), "out": out.path() if out else None}
if do_cook and out is not None:
    out.cook(force=True)
    g = out.geometry()
    if g is not None:
        info["points"] = g.intrinsicValue("pointcount")
        info["prims"] = g.intrinsicValue("primitivecount")
_result = info
''' % (_py_literal(builder), int(shape), int(seed), _py_literal(geo_name),
       "True" if cook else "False")
    return run_python(code, timeout=360)


def screenshot(file_path):
    file_path = os.path.abspath(file_path)
    code = r'''
path = %s
if not hou.isUIAvailable():
    raise RuntimeError("GUI Houdini is required for a viewport screenshot. Start with mode=houdini.")
desktop = hou.ui.curDesktop()
scene = desktop.paneTabOfType(hou.paneTabType.SceneViewer)
if scene is None:
    raise RuntimeError("no Scene Viewer tab")
flip = scene.curViewport()
# hou.GeometryViewport.saveAsImage exists on recent builds.
if hasattr(flip, "saveViewToFile"):
    flip.saveViewToFile(path)
elif hasattr(flip, "saveAsImage"):
    flip.saveAsImage(path)
else:
    raise RuntimeError("this Houdini build cannot save a viewport image from HOM")
_result = {"path": path}
''' % _py_literal(file_path)
    return run_python(code, timeout=60)
