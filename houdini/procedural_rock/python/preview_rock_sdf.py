#!/usr/bin/env python3
"""Sphere-trace the same limestone SDF the Houdini wrangles implement.

Used to tune shape / strata against the two reference photos without Houdini.
Writes PNG files next to this script by default.

    python3 preview_rock_sdf.py
    python3 preview_rock_sdf.py --shape both --width 360
"""

from __future__ import print_function

import argparse
import os
import struct
import sys
import zlib

import numpy as np


# ---------------------------------------------------------------------------
# math
# ---------------------------------------------------------------------------

def _clamp(x, a=0.0, b=1.0):
    return np.clip(x, a, b)


def _smooth(e0, e1, x):
    t = _clamp((x - e0) / np.maximum(e1 - e0, 1e-6))
    return t * t * (3.0 - 2.0 * t)


def _smin(a, b, k):
    h = _clamp(0.5 + 0.5 * (b - a) / np.maximum(k, 1e-6))
    return b * (1.0 - h) + a * h - k * h * (1.0 - h)


def _smax(a, b, k):
    return -_smin(-a, -b, k)


def _length(v, axis=-1):
    return np.sqrt(np.maximum(np.sum(v * v, axis=axis), 0.0))


def _dot(a, b):
    return np.sum(a * b, axis=-1)


def sd_round_box(p, b, r):
    q = np.abs(p) - b + r
    return _length(np.maximum(q, 0.0)) + np.minimum(np.maximum.reduce(q, axis=-1), 0.0) - r


def sd_box2(x, y, bx, by):
    qx = np.abs(x) - bx
    qy = np.abs(y) - by
    return (np.sqrt(np.maximum(qx, 0.0) ** 2 + np.maximum(qy, 0.0) ** 2)
            + np.minimum(np.maximum(qx, qy), 0.0))


def sd_circle2(x, y, r):
    return np.sqrt(x * x + y * y) - r


def sd_ellipse2(x, y, rx, ry):
    k0 = np.sqrt((x / rx) ** 2 + (y / ry) ** 2)
    k1 = np.sqrt((x / (rx * rx)) ** 2 + (y / (ry * ry)) ** 2)
    return k0 * (k0 - 1.0) / np.maximum(k1, 1e-6)


def sd_ellipsoid(p, rad):
    k0 = _length(p / rad)
    k1 = _length(p / (rad * rad))
    return k0 * (k0 - 1.0) / np.maximum(k1, 1e-6)


def sd_uneven_capsule(p, a, b, ra, rb):
    p = p - a
    b = b - a
    baba = np.dot(b, b)
    papa = _dot(p, p)
    paba = _dot(p, b) / max(baba, 1e-6)
    x = np.sqrt(np.maximum(papa - paba * paba * baba, 0.0))
    cax = np.maximum(0.0, x - np.where(paba < 0.5, ra, rb))
    cay = np.abs(paba - 0.5) - 0.5
    rba = rb - ra
    k = rba * rba + baba
    f = _clamp((rba * (x - ra) + paba * baba) / max(k, 1e-6))
    cbx = x - ra - f * rba
    cby = paba - f
    s = np.where((cbx < 0.0) & (cay < 0.0), -1.0, 1.0)
    return s * np.sqrt(np.minimum(cax * cax + cay * cay * baba,
                                  cbx * cbx + cby * cby * baba))


def hash31(p):
    n = np.sin(_dot(p, np.array([127.1, 311.7, 74.7]))) * 43758.5453
    return n - np.floor(n)


def hash33(p):
    n0 = hash31(p)
    n1 = hash31(p + np.array([17.2, 9.1, 3.3]))
    n2 = hash31(p + np.array([5.7, 21.4, 11.8]))
    return np.stack([n0, n1, n2], axis=-1)


def value_noise(p):
    i = np.floor(p)
    f = p - i
    u = f * f * (3.0 - 2.0 * f)
    n = 0.0
    for dx in (0.0, 1.0):
        for dy in (0.0, 1.0):
            for dz in (0.0, 1.0):
                h = hash31(i + np.array([dx, dy, dz]))
                wx = u[..., 0] if dx else (1.0 - u[..., 0])
                wy = u[..., 1] if dy else (1.0 - u[..., 1])
                wz = u[..., 2] if dz else (1.0 - u[..., 2])
                n = n + h * wx * wy * wz
    return n


def fbm(p, octaves=5, roughness=0.48):
    amp = 1.0
    total = 0.0
    norm = 0.0
    q = p
    for _ in range(max(int(octaves), 1)):
        total = total + amp * (value_noise(q) * 2.0 - 1.0)
        norm = norm + amp
        amp *= roughness
        q = q * 2.03
    return total / max(norm, 1e-6)


def worley_f2f1(p):
    i = np.floor(p)
    f = p - i
    f1 = np.full(p.shape[:-1], 1e9)
    f2 = np.full(p.shape[:-1], 1e9)
    for xo in (-1, 0, 1):
        for yo in (-1, 0, 1):
            for zo in (-1, 0, 1):
                off = np.array([xo, yo, zo], dtype=np.float64)
                rnd = hash33(i + off)
                d = _length(f - (rnd + off))
                swap = d < f1
                f2 = np.where(swap, f1, np.minimum(f2, d))
                f1 = np.where(swap, d, f1)
    return f2 - f1


# ---------------------------------------------------------------------------
# SDF matching the four Volume Wrangles
# ---------------------------------------------------------------------------

class RockParams(object):
    def __init__(self, shape=0):
        self.shape = shape
        self.height = 3.6
        self.width = 2.4 if shape == 0 else 1.35
        self.depth = 0.72 if shape == 0 else 1.15
        self.top_taper = 0.72 if shape == 0 else 0.70
        self.base_flare = 1.16 if shape == 0 else 1.05
        self.face_slope = 0.08
        self.lean = 0.04 if shape == 0 else 0.12
        self.roundness = 0.10
        self.smooth_k = 0.20
        self.head_overhang = 0.34
        self.terrace_y = 0.05
        self.seed = 1

        self.warp_amp = 0.22 if shape == 0 else 0.30
        self.warp_freq = 0.48
        self.fbm_amp = 0.12 if shape == 0 else 0.28
        self.fbm_freq = 0.75
        self.fbm_octaves = 5
        self.fbm_rough = 0.46
        self.cell_amp = 0.08 if shape == 0 else 0.20
        self.cell_freq = 1.00
        self.cell_stretch = 0.22
        self.aniso_y = 0.42 if shape == 0 else 0.85

        self.strata_freq = 3.4 if shape == 0 else 2.2
        self.strata_depth = 0.024 if shape == 0 else 0.016
        self.strata_sharp = 6.0
        self.strata_tilt = 0.04
        self.strata_warp = 0.55 if shape == 0 else 0.28
        self.hard_push = 0.35
        self.terrace_depth = 0.045 if shape == 0 else 0.01
        self.crack_freq = 1.85
        self.crack_depth = 0.075 if shape == 0 else 0.035
        self.crack_width = 0.065
        self.crack_stretch = 0.11

        self.pit_amp = 0.016
        self.pit_freq = 6.8
        self.crumb_amp = 0.022
        self.crumb_freq = 3.0
        self.cave_amp = 0.028
        self.cave_freq = 1.35
        self.streak_amp = 0.012
        self.streak_freq = 2.4


def vol_01(p, prm):
    hsh = np.mod(np.sin(prm.seed * 17.23) * 43758.5, 1.0)
    p = p.copy()
    p[..., 0] = p[..., 0] + p[..., 1] * (hsh - 0.5) * 0.07
    lean = prm.lean * (1.0 if prm.shape == 1 else 0.12)
    p[..., 2] = p[..., 2] + p[..., 1] * lean

    ny = _clamp((p[..., 1] + prm.height) / max(2.0 * prm.height, 1e-6))
    taper = prm.base_flare * (1.0 - ny ** 1.05) + prm.top_taper * (ny ** 1.05)
    taper = np.maximum(taper, 0.18)
    q = p.copy()
    q[..., 0] = q[..., 0] / taper

    if prm.shape == 0:
        d2 = sd_box2(q[..., 0], q[..., 1], prm.width * 0.78, prm.height * 0.92) - prm.roundness
        hx = q[..., 0] + prm.width * (0.22 + prm.head_overhang * 0.55)
        hy = q[..., 1] - prm.height * 0.50
        d2 = _smin(d2, sd_ellipse2(hx, hy, prm.width * 0.70, prm.height * 0.40), prm.smooth_k)
        bx = q[..., 0] + prm.width * (0.72 + prm.head_overhang * 0.55)
        by = q[..., 1] - prm.height * 0.64
        d2 = _smin(d2, sd_ellipse2(bx, by, prm.width * 0.48, prm.height * 0.17), prm.smooth_k)
        d2 = _smin(d2, sd_ellipse2(q[..., 0] + prm.width * 0.12,
                                  q[..., 1] - prm.height * 1.00,
                                  prm.width * 0.50, prm.height * 0.20), prm.smooth_k)
        d2 = _smin(d2, sd_box2(q[..., 0], q[..., 1] + prm.height * 0.84,
                               prm.width * 1.05, prm.height * 0.26) - prm.roundness,
                   prm.smooth_k)
        terr = 1.0 - _smooth(0.0, prm.height * 0.18, np.abs(q[..., 1] - prm.terrace_y))
        left = 1.0 - _smooth(-prm.width * 0.15, prm.width * 0.65, q[..., 0])
        d2 = d2 + terr * left * 0.14
        d2 = d2 + fbm(np.stack([q[..., 0] * 0.48 + prm.seed,
                                q[..., 1] * 0.48,
                                np.zeros_like(q[..., 0])], -1), 4, 0.5) * 0.38

        half_z = prm.depth * (1.08 * (1.0 - ny) + 0.78 * ny)
        face_z = q[..., 2] - prm.face_slope * (q[..., 1] + prm.height) * 0.15
        slab = np.abs(face_z) - half_z
        d = _smax(d2, slab, 0.14)
        d = _smax(d, -q[..., 2] - prm.depth * 1.15, 0.18)
    else:
        q[..., 2] = q[..., 2] / np.maximum(taper * 0.96, 0.14)
        d = sd_ellipsoid(q, np.array([prm.width * 0.58, prm.height * 0.88, prm.depth * 0.50]))
        tip = q - np.array([prm.width * 0.28, prm.height * 0.68, -prm.depth * 0.05])
        d = _smin(d, sd_ellipsoid(tip, np.array([prm.width * 0.30, prm.height * 0.46, prm.depth * 0.26])),
                  prm.smooth_k)
        sh_c = q - np.array([prm.width * 0.40, -prm.height * 0.18, 0.0])
        d = _smin(d, sd_ellipsoid(sh_c, np.array([prm.width * 0.50, prm.height * 0.52, prm.depth * 0.38])),
                  prm.smooth_k)
        face = (q[..., 0] * 0.18 + q[..., 2] - prm.depth * 0.22
                + ny * prm.face_slope * prm.height * 0.22)
        d = _smax(d, face, prm.smooth_k * 1.1)

    ground = p[..., 1] + prm.height
    d = _smax(d, -ground, 0.20)
    return d


def vol_02(p, d, prm):
    p = p + np.array([prm.seed * 0.17, prm.seed * 0.31, prm.seed * 0.11])
    pa = p.copy()
    pa[..., 1] = pa[..., 1] * prm.aniso_y

    warp = np.stack([
        fbm(pa * prm.warp_freq + np.array([3.1, 0, 0]), 3, 0.5),
        fbm(pa * prm.warp_freq + np.array([0, 7.7, 0]), 3, 0.5),
        fbm(pa * prm.warp_freq + np.array([0, 0, 2.4]), 3, 0.5),
    ], axis=-1) * prm.warp_amp
    q = pa + warp
    macro = fbm(q * prm.fbm_freq, prm.fbm_octaves, prm.fbm_rough) * prm.fbm_amp
    cw = q.copy()
    cw[..., 1] = cw[..., 1] * prm.cell_stretch
    cells = (worley_f2f1(cw * prm.cell_freq) * 2.0 - 0.7) * prm.cell_amp
    return d + macro + cells


def vol_03(p, d, prm):
    above = _smooth(prm.terrace_y - 0.35, prm.terrace_y + 0.55, p[..., 1])
    below = 1.0 - above * 0.55
    bed_y = (p[..., 1] + p[..., 2] * prm.strata_tilt
             + fbm(np.stack([p[..., 0], np.zeros_like(p[..., 0]), p[..., 2]], -1) * 0.62
                   + prm.seed, 3, 0.5) * prm.strata_warp)
    bed_y = bed_y + 0.18 * np.sin(bed_y * 1.7 + prm.seed * 0.4)
    wave = np.sin(bed_y * prm.strata_freq * 6.28318)
    groove = np.power(np.maximum(1.0 - np.abs(wave), 0.0), prm.strata_sharp)
    d = d + groove * prm.strata_depth * (0.40 + 0.60 * above)
    soft = np.power(np.maximum(-wave, 0.0), 1.35)
    hard = np.power(np.maximum(wave, 0.0), 2.10)
    d = d + soft * prm.strata_depth * 0.85 * (0.30 + 0.70 * above)
    d = d - hard * prm.strata_depth * prm.hard_push * (0.25 + 0.75 * above)
    pack = np.abs(np.sin(bed_y * prm.strata_freq * 0.31 + prm.seed))
    d = d + np.power(pack, 9.0) * prm.strata_depth * 0.70 * above
    terr = 1.0 - _smooth(0.0, 0.22, np.abs(p[..., 1] - prm.terrace_y))
    d = d + terr * prm.terrace_depth

    jp = p.copy()
    jp[..., 1] = jp[..., 1] * prm.crack_stretch
    jp = jp * prm.crack_freq + np.array([prm.seed * 0.19, 0.0, prm.seed * 0.41])
    edge = worley_f2f1(jp)
    joint = 1.0 - _smooth(0.0, prm.crack_width, edge)
    d = d + joint * prm.crack_depth * below

    ca, sa = 0.8, 0.6
    rp = np.stack([
        p[..., 0] * ca - p[..., 2] * sa,
        p[..., 1] * prm.crack_stretch,
        p[..., 0] * sa + p[..., 2] * ca,
    ], axis=-1)
    edge2 = worley_f2f1(rp * prm.crack_freq * 0.70 + 4.2)
    joint2 = 1.0 - _smooth(0.0, prm.crack_width * 1.25, edge2)
    d = d + joint2 * prm.crack_depth * 0.40 * below
    return d


def vol_04(p, d, prm):
    p = p + prm.seed * 0.07
    d = d + fbm(p * prm.pit_freq, 4, 0.55) * prm.pit_amp
    d = d + fbm(p * prm.crumb_freq + np.array([11.0, 0, 0]), 3, 0.5) * prm.crumb_amp
    cave_n = value_noise(p * prm.cave_freq + 19.0)
    d = d + _smooth(0.74, 0.93, cave_n) * prm.cave_amp
    sp = p.copy()
    sp[..., 1] = sp[..., 1] * 0.18
    d = d + fbm(sp * prm.streak_freq + np.array([0, prm.seed, 0]), 3, 0.45) * prm.streak_amp
    return d


def rock_sdf(p, prm):
    d = vol_01(p, prm)
    d = vol_02(p, d, prm)
    d = vol_03(p, d, prm)
    d = vol_04(p, d, prm)
    return d


def rock_normal(p, prm, eps=0.012):
    e = np.array([
        [eps, 0, 0],
        [0, eps, 0],
        [0, 0, eps],
    ])
    dx = rock_sdf(p + e[0], prm) - rock_sdf(p - e[0], prm)
    dy = rock_sdf(p + e[1], prm) - rock_sdf(p - e[1], prm)
    dz = rock_sdf(p + e[2], prm) - rock_sdf(p - e[2], prm)
    n = np.stack([dx, dy, dz], axis=-1)
    return n / np.maximum(_length(n)[..., None], 1e-6)


# ---------------------------------------------------------------------------
# renderer
# ---------------------------------------------------------------------------

def write_png(path, rgb):
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[i].tobytes() for i in range(h))

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(png)


def look_at(eye, target, up=np.array([0.0, 1.0, 0.0])):
    f = target - eye
    f = f / np.linalg.norm(f)
    r = np.cross(f, up)
    r = r / np.linalg.norm(r)
    u = np.cross(r, f)
    return f, r, u


def render(prm, width=360, height=480, eye=None, target=None, sun=None, fov=0.42):
    if eye is None:
        if prm.shape == 0:
            eye = np.array([1.70, 0.05, 7.6])
            target = np.array([-0.35, 0.20, 0.0])
            sun = np.array([-0.42, 0.58, 0.70])
        else:
            eye = np.array([-2.0, 1.15, 7.4])
            target = np.array([0.15, 0.20, 0.0])
            sun = np.array([0.55, 0.48, 0.68])
    sun = sun / np.linalg.norm(sun)
    fwd, right, up = look_at(eye, target)

    ys, xs = np.mgrid[0:height, 0:width]
    px = (xs + 0.5) / width * 2.0 - 1.0
    py = 1.0 - (ys + 0.5) / height * 2.0
    aspect = width / float(height)
    dirs = (fwd[None, None, :]
            + right[None, None, :] * (px * aspect * fov)[..., None]
            + up[None, None, :] * (py * fov)[..., None])
    dirs = dirs / np.maximum(_length(dirs)[..., None], 1e-6)

    pos = np.broadcast_to(eye, dirs.shape).copy()
    alive = np.ones(dirs.shape[:2], dtype=bool)
    t = np.zeros(dirs.shape[:2])
    hit = np.zeros(dirs.shape[:2], dtype=bool)

    for _ in range(110):
        if not alive.any():
            break
        p = pos[alive]
        d = rock_sdf(p, prm)
        step = np.clip(d * 0.55, 0.008, 0.22)
        t[alive] = t[alive] + step
        pos[alive] = pos[alive] + dirs[alive] * step[..., None]
        newly = d < 0.022
        idx = np.where(alive)
        hit_idx = (idx[0][newly], idx[1][newly])
        hit[hit_idx] = True
        alive[hit_idx] = False
        alive[t > 28.0] = False

    n = np.zeros_like(pos)
    if hit.any():
        n[hit] = rock_normal(pos[hit], prm)

    # albedo: same idea as pt_05
    h01 = _clamp((pos[..., 1] - (-prm.height)) / max(2.0 * prm.height, 1e-6))
    terrace = 0.50
    split = _smooth(terrace - 0.04, terrace + 0.07, h01)
    dirt = np.array([0.27, 0.26, 0.25])
    face = np.array([0.40, 0.38, 0.36])
    pale = np.array([0.84, 0.80, 0.66])
    ledge = np.array([0.88, 0.84, 0.70])
    if prm.shape == 1:
        dirt = np.array([0.55, 0.52, 0.47])
        face = np.array([0.62, 0.59, 0.54])
        pale = np.array([0.70, 0.67, 0.60])
        ledge = np.array([0.78, 0.74, 0.66])
    base = dirt + (face - dirt) * np.clip(h01 / terrace, 0, 1)[..., None]
    base = base * (1.0 - split[..., None]) + pale * split[..., None]
    upn = _clamp(n[..., 1])
    base = base * (1.0 - (np.power(1.0 - upn, 1.6) * 0.18)[..., None]) + face * 0.88 * (np.power(1.0 - upn, 1.6) * 0.18)[..., None]
    albedo = base * (1.0 - np.power(upn, 2.4)[..., None] * 0.55) + ledge * (np.power(upn, 2.4) * 0.55)[..., None]

    ndotl = _clamp(_dot(n, sun))
    wrap = _clamp(_dot(n, sun) * 0.5 + 0.5)
    sky = np.array([0.55, 0.68, 0.88])
    amb = 0.22 + 0.20 * upn
    shade = (0.12 + 0.88 * ndotl)[..., None] * albedo + (amb * 0.18)[..., None] * sky
    shade = shade + (wrap * 0.08)[..., None] * np.array([0.95, 0.80, 0.55])

    # sky background matching the refs
    v = dirs[..., 1]
    if prm.shape == 0:
        bg = np.array([0.45, 0.68, 0.92]) * (0.75 + 0.35 * (v + 1.0) * 0.5)[..., None]
        bg = bg + np.array([0.18, 0.10, 0.02]) * np.maximum(1.0 - (v + 0.15) * 2.0, 0)[..., None]
    else:
        bg = np.array([0.78, 0.84, 0.88]) * (0.85 + 0.20 * (v + 1.0) * 0.5)[..., None]

    # simple ground plane so the mass sits like the photos
    gy = -prm.height
    den = np.where(np.abs(dirs[..., 1]) < 1e-5, 1e-5, dirs[..., 1])
    tg = (gy - eye[1]) / den
    ghit = (tg > 0.0) & (tg < 28.0) & (~hit)
    gpt = eye + dirs * tg[..., None]
    gn = np.array([0.0, 1.0, 0.0])
    gndot = _clamp(np.dot(gn, sun))
    grass = np.array([0.42, 0.46, 0.32]) if prm.shape == 0 else np.array([0.55, 0.56, 0.42])
    gcol = grass * (0.25 + 0.75 * gndot)
    rgb = np.where(hit[..., None], shade, bg)
    rgb = np.where(ghit[..., None], gcol, rgb)
    rgb = np.clip(rgb, 0.0, 1.0)
    rgb = np.power(rgb, 0.92)  # mild display gamma
    return (rgb * 255.0).astype(np.uint8)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", choices=("cliff", "pinnacle", "both"), default="both")
    parser.add_argument("--width", type=int, default=360)
    parser.add_argument("--outdir", default="")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    outdir = args.outdir or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "preview")
    )
    os.makedirs(outdir, exist_ok=True)

    jobs = []
    if args.shape in ("cliff", "both"):
        jobs.append(("cliff", 0, int(args.width), int(args.width * 4 / 3)))
    if args.shape in ("pinnacle", "both"):
        jobs.append(("pinnacle", 1, int(args.width), int(args.width * 1.25)))

    paths = []
    for name, shape, w, h in jobs:
        prm = RockParams(shape=shape)
        prm.seed = args.seed
        print("render %s %dx%d ..." % (name, w, h), file=sys.stderr)
        img = render(prm, width=w, height=h)
        path = os.path.join(outdir, "%s.png" % name)
        write_png(path, img)
        print("wrote", path, file=sys.stderr)
        paths.append(path)
    return paths


if __name__ == "__main__":
    main()
