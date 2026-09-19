# Процедурная известняковая скала (OpenVDB → полигоны)

Сеть SOP в Houdini: signed-distance VDB, четыре Volume Wrangle, затем `Convert VDB` в полигоны.

Референсы: слоистая известняковая стена с горизонтальными пластами и вертикальными трещинами; отдельно стоящий карстовый пинакль.

## Зачем VDB, а не displace по нормалям

Gatis Kurzemnieks ([RenderEverything, part 2](https://www.rendereverything.com/game_assets_with_houdini_2/)) и разбор 80.lv: сдвигайте **значение SDF-вокселя**, не точки меша. Самопересечения остаются валидными, пласты и трещины остаются объёмными.

Что использовано:

| Идея | Источник |
| --- | --- |
| Примитивы SDF + smooth CSG | [Inigo Quilez, distance functions](https://iquilezles.org/articles/distfunctions/) |
| FBM / domain warp на имплицитных скалах | IQ [domain warp](https://iquilezles.org/articles/warp/) + Entagma-style volume noise |
| Worley F2−F1, растянутый по Y | [80.lv — Procedural Rock Generation](https://80.lv/articles/006sdf-breakdown-procedural-rock-generation-in-houdini) |
| Пласты / differential weathering | Kurzemnieks + Matt Ebb layers; implicit strata (Paris / Gain 2019) |
| Joints как стенки ячеек | Saber Jlassi (SIGGRAPH Hive) Voronoi, дешёвые рёбра Worley |
| Нативная замена wrangle 2 | [Volume Noise SDF SOP](https://www.sidefx.com/docs/houdini/nodes/sop/volumenoisesdf.html) |

## Сеть

```
box (bounds, больше скалы)
  └─ vdbfrompolygons          Distance VDB `surface`, Fill Interior, voxel ~0.035
       └─ vol_01_base_mass    стена (shape 0) или пинакль (shape 1)
            └─ vol_02_macro   domain warp + FBM + stretched Worley
                 └─ vol_03    пласты (hard/soft) + две семьи трещин
                      └─ vol_04   pitting / crumbs / alcoves / streaks
                           └─ vdbsmoothsdf   1 iteration
                                └─ convertvdb   Polygons, iso 0, adaptivity 0.015
                                     └─ normal   point normals
                                          └─ pt_05_mesh_lookdev
                                               └─ OUT_ROCK
```

Вставьте каждый файл из `vex/` в соответствующий Wrangle. Или запустите `python/build_rock_network.py` внутри Houdini.

## Ручная сборка

1. Tab → **Box**. Size около `7 9.5 5.5`.
2. **VDB from Polygons**
   - Distance VDB on, имя `surface`
   - Fog off
   - **Fill Interior** on
   - Voxel Size `0.035` (превью) или `0.02` (финал)
   - Exterior band ≥ `16`, Interior ≥ `12`  
     Сумма амплитуд смещения должна оставаться **меньше** зазора скала→box (или `voxel_size * band`).
3. Четыре **Volume Wrangle** по порядку. Snippet = файл `.vex`.  
   Если есть галка Signed-Flood Fill / Fill SDF — включите.
4. **VDB Smooth SDF**, 1 итерация.
5. **Convert VDB** → Polygons, isovalue `0`.
6. **Normal SOP** (points).
7. **Attribute Wrangle** (Points) = `pt_05_mesh_lookdev.vex`.

Опционально вместо wrangle 2: **Volume Noise SDF** (Fast / Worley F2-F1). Пласты и трещины лучше оставить в wrangle 3–4.

## Параметры под фото

**Стена (фото 1)** — `shape = 0`

- `height 3.6` `width 2.4` `depth 0.72` `head_overhang 0.34` `terrace_y 0.05`
- `strata_freq 3.4` `strata_depth 0.024` `hard_push 0.35` `strata_warp 0.55`
- `aniso_y 0.42` `cell_stretch 0.22` `crack_stretch 0.11`
- lookdev `terrace_01 0.52` — тёмный низ, кремовый верх

**Пинакль (фото 2)** — `shape = 1`

- `height 3.6` `width 1.35` `depth 1.15` `lean 0.12` `top_taper 0.48`
- `strata_depth 0.016` `fbm_amp 0.28` `cell_amp 0.20` `aniso_y 0.85`
- слабее трещины: `crack_depth 0.035`

Меняйте `seed` на всех четырёх volume wrangle вместе — новый вариант той же породы.

Скрипт сборки по умолчанию ставит стену в `/obj/procedural_rock`. Второй объект:

```
build(geo_name="procedural_pinnacle", shape=1)
```

## Файлы

| Файл | Нода |
| --- | --- |
| `vex/include/rock_sdf.h` | общая библиотека (опциональный `#include`) |
| `vex/vol_01_base_mass.vex` | Volume Wrangle |
| `vex/vol_02_macro_displace.vex` | Volume Wrangle |
| `vex/vol_03_strata_cracks.vex` | Volume Wrangle |
| `vex/vol_04_weathering.vex` | Volume Wrangle |
| `vex/pt_05_mesh_lookdev.vex` | Attribute Wrangle, Points |
| `python/build_rock_network.py` | собирает `/obj/procedural_rock` |
| `python/preview_rock_sdf.py` | сфертрейс того же SDF без Houdini |
| [`../mcp/README.md`](../mcp/README.md) | MCP sidecar: Cursor запускает Houdini на вашем ПК |
| `preview/cliff.png` | превью стены (shape 0) |
| `preview/pinnacle.png` | превью пинакля (shape 1) |
