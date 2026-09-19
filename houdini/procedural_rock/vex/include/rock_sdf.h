#ifndef ROCK_SDF_H
#define ROCK_SDF_H

// Shared SDF helpers for procedural limestone / karst cliffs.
// Sources: Inigo Quilez distance functions; Kurzemnieks VDB offset
// (displace f@surface, do not push mesh points).

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
    vector q = abs(p) - b + set(r, r, r);
    return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0) - r;
}

float sk_sd_ellipsoid(vector p; vector r)
{
    float k0 = length(p / r);
    float k1 = length(p / (r * r));
    return k0 * (k0 - 1.0) / max(k1, 1e-6);
}

float sk_sd_box2(float x; float y; float bx; float by)
{
    float qx = abs(x) - bx;
    float qy = abs(y) - by;
    return sqrt(max(qx, 0.0) * max(qx, 0.0) + max(qy, 0.0) * max(qy, 0.0))
         + min(max(qx, qy), 0.0);
}

float sk_sd_capsule(vector p; vector a; vector b; float r)
{
    vector pa = p - a;
    vector ba = b - a;
    float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-6), 0.0, 1.0);
    return length(pa - ba * h) - r;
}

// IQ uneven capsule: different radii at endpoints a / b.
float sk_sd_uneven_capsule(vector p; vector a; vector b; float ra; float rb)
{
    p -= a;
    b -= a;
    float baba = dot(b, b);
    float papa = dot(p, p);
    float paba = dot(p, b) / max(baba, 1e-6);
    float x = sqrt(max(papa - paba * paba * baba, 0.0));
    float cax = max(0.0, x - ((paba < 0.5) ? ra : rb));
    float cay = abs(paba - 0.5) - 0.5;
    float rba = rb - ra;
    float k = rba * rba + baba;
    float f = clamp((rba * (x - ra) + paba * baba) / max(k, 1e-6), 0.0, 1.0);
    float cbx = x - ra - f * rba;
    float cby = paba - f;
    float s = (cbx < 0.0 && cay < 0.0) ? -1.0 : 1.0;
    return s * sqrt(min(cax * cax + cay * cay * baba,
                        cbx * cbx + cby * cby * baba));
}

float sk_hash31(vector p)
{
    return frac(sin(dot(p, set(127.1, 311.7, 74.7))) * 43758.5453);
}

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

float sk_worley_f2f1(vector p)
{
    float f1, f2;
    wnoise(p, f1, f2);
    return f2 - f1;
}

#endif
