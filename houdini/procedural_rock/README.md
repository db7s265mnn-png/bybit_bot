# Procedural limestone cliff (OpenVDB → polygons)

Houdini SOP network: signed-distance VDB, four Volume Wrangles, then `Convert VDB` to polygons.

References: layered limestone wall with horizontal beds and vertical joints; isolated karst pinnacle.

## Why VDB, not mesh displace

Gatis Kurzemnieks ([RenderEverything, part 2](https://www.rendereverything.com/game_assets_with_houdini_2/)) and the 80.lv rock breakdown: offset the **SDF voxel value**, do not push points along normals. Self-intersections stay valid, strata and cracks stay volumetric.

Building blocks used here:

| Idea | Source |
| --- | --- |
| Primitive SDF + smooth CSG | [Inigo Quilez, distance functions](https://iquilezles.org/articles/distfunctions/) |
| FBM / domain warp on implicit rocks | IQ + Entagma-style volume noise |
| Worley F2−F1, stretched in Y | [80.lv — Procedural Rock Generation](https://80.lv/articles/006sdf-breakdown-procedural-rock-generation-in-houdini) |
| Bedding planes / strata | Kurzemnieks + Matt Ebb layer distribution, implemented as periodic SDF grooves |
| Joints as cell walls | Saber Jlassi (SIGGRAPH Hive) Voronoi idea, cheap Worley edges |
| Native alternative | [Volume Noise SDF SOP](https://www.sidefx.com/docs/houdini/nodes/sop/volumenoisesdf.html) |

## Network

```
box (bounds, bigger than the rock)
  └─ vdbfrompolygons          Distance VDB `surface`, Fill Interior, voxel ~0.035
       └─ vol_01_base_mass    cliff slab or pinnacle ellipsoid
            └─ vol_02_macro   domain warp + FBM + stretched Worley
                 └─ vol_03    horizontal strata + two joint families
                      └─ vol_04   pitting / crumbs / alcoves
                           └─ vdbsmoothsdf   1 iteration
                                └─ convertvdb   Polygons, iso 0, adaptivity 0.015
                                     └─ normal   point normals
                                          └─ pt_05_mesh_lookdev
                                               └─ OUT_ROCK
```

Paste each file from `vex/` into the matching Wrangle. Or run `python/build_rock_network.py` inside Houdini.

## Manual setup

1. Tab → **Box**. Size about `6 8.5 4.5`.
2. **VDB from Polygons**
   - Distance VDB on, name `surface`
   - Fog off
   - **Fill Interior** on
   - Voxel Size `0.035` (preview) or `0.02` (final)
   - Exterior / Interior band ≥ `8` voxels  
     Displacement amplitude must stay **below** `voxel_size * band`.
3. Four **Volume Wrangle** nodes in order. Snippet = the `.vex` file.  
   Enable **Signed-Flood Fill Output SDF VDBs**.
4. **VDB Smooth SDF**, 1 iteration.
5. **Convert VDB** → Polygons, isovalue `0`.
6. **Normal SOP** (points).
7. **Attribute Wrangle** (Points) = `pt_05_mesh_lookdev.vex`.

Optional native nodes instead of wrangle 2: **Volume Noise SDF** (Fast / Worley F2-F1). Keep wrangles 3–4 for beds and cracks.

## Parameters that match the photos

**Cliff wall (photo 1)** — `shape = 0`

- `height 3.6` `width 2.4` `depth 1.15`
- `strata_freq 4.5` `strata_depth 0.055`
- `crack_stretch 0.12` (joints stand vertical)
- `cell_stretch 0.28`

**Pinnacle (photo 2)** — `shape = 1`

- `height 3.8` `width 1.35` `depth 1.2` `lean 0.12`
- Lower `strata_depth` (~0.03) — the second ref is more massive, fewer ledges
- Slightly higher `fbm_amp` (~0.28)

Change `seed` on all four volume wrangles together for a new look.

## Files

| File | Node |
| --- | --- |
| `vex/include/rock_sdf.h` | shared library (optional `#include`) |
| `vex/vol_01_base_mass.vex` | Volume Wrangle |
| `vex/vol_02_macro_displace.vex` | Volume Wrangle |
| `vex/vol_03_strata_cracks.vex` | Volume Wrangle |
| `vex/vol_04_weathering.vex` | Volume Wrangle |
| `vex/pt_05_mesh_lookdev.vex` | Attribute Wrangle, Points |
| `python/build_rock_network.py` | builds `/obj/procedural_rock` |
