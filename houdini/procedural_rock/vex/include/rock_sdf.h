#ifndef ROCK_SDF_H
#define ROCK_SDF_H

// Shared SDF helpers for procedural limestone / karst cliffs.
// Based on Inigo Quilez distance functions + Houdini VDB rock practice:
// displace the SDF in voxel space instead of offsetting mesh points.

float sk_smin(float a; float b; float k)
{
    if (k <= 1e-6)
        return min(a, b);
    float h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0);
    return lerp(b, a, h) - k * h * (1.0 - h);
}

float sk_smax(float a; float b; float k)
{
    return -sk_smin(-a, -b, k);
}

float sk_sd_sphere(vector p; float r)
{
    return length(p) - r;
}

float sk_sd_box(vector p; vector b)
{
    vector q = abs(p) - b;
    return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0);
}

float sk_sd_round_box(vector p; vector b; float r)
{
    return sk_sd_box(p, b) - r;
}

float sk_sd_ellipsoid(vector p; vector r)
{
    float k0 = length(p / r);
    float k1 = length(p / (r * r));
    return k0 * (k0 - 1.0) / max(k1, 1e-6);
}

float sk_sd_plane(vector p; vector n; float h)
{
    return dot(p, normalize(n)) + h;
}

float sk_sd_capsule(vector p; vector a; vector b; float r)
{
    vector pa = p - a;
    vector ba = b - a;
    float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-6), 0.0, 1.0);
    return length(pa - ba * h) - r;
}

float sk_hash31(vector p)
{
    return frac(sin(dot(p, set(127.1, 311.7, 74.7))) * 43758.5453);
}

// Perlin-ish FBM in [-1, 1].
float sk_fbm(vector p; int octaves; float roughness)
{
    float amp = 1.0;
    float sum = 0.0;
    float norm = 0.0;
    vector q = p;
    int n = max(octaves, 1);
    for (int i = 0; i < n; i++)
    {
        sum += amp * (noise(q) * 2.0 - 1.0);
        norm += amp;
        amp *= roughness;
        q *= 2.03;
    }
    return sum / max(norm, 1e-6);
}

// Worley F2-F1: blocky cells, good for jointed rock.
float sk_worley_f2f1(vector p)
{
    float f1, f2;
    wnoise(p, f1, f2);
    return f2 - f1;
}

#endif
