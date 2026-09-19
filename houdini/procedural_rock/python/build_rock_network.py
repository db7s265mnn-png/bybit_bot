"""
Build /obj/procedural_rock : VDB limestone cliff -> polygons.

Run inside Houdini:
    Python Source Editor, or
    Windows > Python Shell:

        exec(open(r"/absolute/path/houdini/procedural_rock/python/build_rock_network.py").read())

The script inlines the VEX files next to this repo so you do not need HOUDINI_VEX_PATH.
"""

from __future__ import print_function

import os

import hou


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VEX = os.path.join(ROOT, "vex")


def _read(name):
    path = os.path.join(VEX, name)
    with open(path, "r") as handle:
        return handle.read()


def _spare_float(node, name, default, label, minval=-10.0, maxval=10.0):
    if node.parm(name):
        node.parm(name).set(default)
        return
    tmpl = hou.FloatParmTemplate(name, label, 1, default_value=(default,),
                                 min=minval, max=maxval)
    group = node.parmTemplateGroup()
    group.append(tmpl)
    node.setParmTemplateGroup(group)
    node.parm(name).set(default)


def _spare_int(node, name, default, label, minval=0, maxval=16):
    if node.parm(name):
        node.parm(name).set(default)
        return
    tmpl = hou.IntParmTemplate(name, label, 1, default_value=(default,),
                               min=minval, max=maxval)
    group = node.parmTemplateGroup()
    group.append(tmpl)
    node.setParmTemplateGroup(group)
    node.parm(name).set(default)


def _spare_vec(node, name, default, label):
    if node.parm(name):
        node.parmTuple(name).set(default)
        return
    tmpl = hou.FloatParmTemplate(name, label, 3, default_value=default)
    group = node.parmTemplateGroup()
    group.append(tmpl)
    node.setParmTemplateGroup(group)
    node.parmTuple(name).set(default)


def _set_if(node, name, value):
    parm = node.parm(name)
    if parm is not None:
        parm.set(value)


def build(parent_path="/obj", geo_name="procedural_rock"):
    parent = hou.node(parent_path)
    if parent is None:
        raise RuntimeError("Missing %s" % parent_path)

    geo = hou.node("%s/%s" % (parent_path, geo_name))
    if geo:
        geo.destroy()
    geo = parent.createNode("geo", geo_name)
    geo.moveToGoodPosition()

    for child in geo.children():
        if child.name() in ("file1", "file"):
            child.destroy()

    box = geo.createNode("box", "bounds")
    box.parmTuple("size").set((6.0, 8.5, 4.5))
    box.parmTuple("t").set((0.0, 0.0, 0.0))

    vdb = geo.createNode("vdbfrompolygons", "vdb_from_bounds")
    vdb.setInput(0, box)
    _set_if(vdb, "distancevdb", True)
    _set_if(vdb, "fogvdb", False)
    _set_if(vdb, "surfacevdbname", "surface")
    _set_if(vdb, "voxelsize", 0.035)
    _set_if(vdb, "exteriorband", 8)
    _set_if(vdb, "interiorband", 8)
    _set_if(vdb, "fillinterior", True)
    _set_if(vdb, "sdfstandard", "ws")

    def vol_wrangle(name, filename):
        node = geo.createNode("volumewrangle", name)
        node.parm("snippet").set(_read(filename))
        _set_if(node, "fillsdf", True)
        _set_if(node, "signedflood", True)
        _set_if(node, "voxelsample", "center")
        return node

    w1 = vol_wrangle("vol_01_base_mass", "vol_01_base_mass.vex")
    w1.setInput(0, vdb)
    _spare_int(w1, "shape", 0, "Shape 0=cliff 1=pinnacle", 0, 1)
    _spare_float(w1, "height", 3.6, "Height", 0.5, 12)
    _spare_float(w1, "width", 2.4, "Width", 0.3, 8)
    _spare_float(w1, "depth", 1.15, "Depth", 0.2, 6)
    _spare_float(w1, "top_taper", 0.55, "Top taper", 0.1, 1.5)
    _spare_float(w1, "base_flare", 1.18, "Base flare", 0.5, 2.5)
    _spare_float(w1, "face_slope", 0.12, "Face slope", -0.5, 0.8)
    _spare_float(w1, "lean", 0.08, "Lean", -0.5, 0.5)
    _spare_float(w1, "roundness", 0.16, "Roundness", 0.0, 1.0)
    _spare_float(w1, "smooth_k", 0.18, "Smooth K", 0.0, 1.0)
    _spare_int(w1, "seed", 1, "Seed", 0, 9999)

    w2 = vol_wrangle("vol_02_macro_displace", "vol_02_macro_displace.vex")
    w2.setInput(0, w1)
    _spare_float(w2, "warp_amp", 0.35, "Warp amp", 0, 2)
    _spare_float(w2, "warp_freq", 0.55, "Warp freq", 0.01, 8)
    _spare_float(w2, "fbm_amp", 0.22, "FBM amp", 0, 1.5)
    _spare_float(w2, "fbm_freq", 0.85, "FBM freq", 0.01, 8)
    _spare_int(w2, "fbm_octaves", 5, "FBM octaves", 1, 8)
    _spare_float(w2, "fbm_rough", 0.48, "FBM rough", 0.1, 0.95)
    _spare_float(w2, "cell_amp", 0.16, "Worley amp", 0, 1.5)
    _spare_float(w2, "cell_freq", 1.15, "Worley freq", 0.01, 8)
    _spare_float(w2, "cell_stretch", 0.28, "Worley Y stretch", 0.02, 2)
    _spare_int(w2, "seed", 1, "Seed", 0, 9999)

    w3 = vol_wrangle("vol_03_strata_cracks", "vol_03_strata_cracks.vex")
    w3.setInput(0, w2)
    _spare_float(w3, "strata_freq", 4.5, "Strata freq", 0.1, 20)
    _spare_float(w3, "strata_depth", 0.055, "Strata depth", 0, 0.4)
    _spare_float(w3, "strata_sharp", 6.0, "Strata sharp", 1, 20)
    _spare_float(w3, "strata_tilt", 0.04, "Strata tilt", -0.4, 0.4)
    _spare_float(w3, "strata_warp", 0.35, "Strata warp", 0, 2)
    _spare_float(w3, "crack_freq", 1.7, "Crack freq", 0.1, 10)
    _spare_float(w3, "crack_depth", 0.07, "Crack depth", 0, 0.5)
    _spare_float(w3, "crack_width", 0.07, "Crack width", 0.01, 0.4)
    _spare_float(w3, "crack_stretch", 0.12, "Crack Y stretch", 0.01, 1)
    _spare_int(w3, "seed", 1, "Seed", 0, 9999)

    w4 = vol_wrangle("vol_04_weathering", "vol_04_weathering.vex")
    w4.setInput(0, w3)
    _spare_float(w4, "pit_amp", 0.018, "Pit amp", 0, 0.2)
    _spare_float(w4, "pit_freq", 6.5, "Pit freq", 0.2, 24)
    _spare_float(w4, "crumb_amp", 0.025, "Crumb amp", 0, 0.2)
    _spare_float(w4, "crumb_freq", 3.2, "Crumb freq", 0.2, 16)
    _spare_float(w4, "cave_amp", 0.03, "Alcove amp", 0, 0.3)
    _spare_float(w4, "cave_freq", 1.4, "Alcove freq", 0.1, 8)
    _spare_int(w4, "seed", 1, "Seed", 0, 9999)

    smooth = geo.createNode("vdbsmoothsdf", "vdb_smooth")
    smooth.setInput(0, w4)
    _set_if(smooth, "iterations", 1)
    _set_if(smooth, "type", "mean")

    convert = geo.createNode("convertvdb", "convert_to_polygons")
    convert.setInput(0, smooth)
    _set_if(convert, "conversion", "poly")
    _set_if(convert, "adaptivity", 0.015)
    _set_if(convert, "isovalue", 0.0)
    _set_if(convert, "vdbclass", "sdf")

    nrm = geo.createNode("normal", "normals")
    nrm.setInput(0, convert)
    _set_if(nrm, "type", "typepoint")

    look = geo.createNode("attribwrangle", "pt_05_mesh_lookdev")
    look.parm("snippet").set(_read("pt_05_mesh_lookdev.vex"))
    class_parm = look.parm("class")
    if class_parm is not None:
        try:
            class_parm.set("point")
        except hou.OperationFailed:
            class_parm.set(2)
    look.setInput(0, nrm)
    _spare_float(look, "y_min", -3.6, "Y min", -20, 20)
    _spare_float(look, "y_max", 3.6, "Y max", -20, 20)
    _spare_float(look, "ledge_gamma", 2.1, "Ledge gamma", 0.2, 6)
    _spare_vec(look, "dirt_color", (0.34, 0.31, 0.27), "Dirt")
    _spare_vec(look, "face_color", (0.55, 0.53, 0.50), "Face")
    _spare_vec(look, "ledge_color", (0.80, 0.76, 0.66), "Ledge")

    out = geo.createNode("null", "OUT_ROCK")
    out.setInput(0, look)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)

    geo.layoutChildren()
    print("Built %s/%s" % (parent_path, geo_name))
    return geo


if __name__ == "__main__":
    build()
