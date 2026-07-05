# Phantasy Star Online — Model & Stage Format Specification

This document describes, at the byte level, every file format supported by the PSO Ultimate Importer Blender addon. It covers three PSO platform variants — **Dreamcast (DC)**, **PC / Blue Burst (BB)**, and **GameCube (GC)**, including Episode III: C.A.R.D. Revolution — and is written so a programmer with no access to the addon's source code could implement a compatible parser or converter from this document alone.

All multi-byte integers and floats are stored **little-endian** unless a section states otherwise. GameCube (PowerPC) files are **big-endian** almost everywhere; this is called out per-format.

## Table of Contents

1. [Shared Utilities & Conventions](#1-shared-utilities--conventions)
2. [NJ — Dreamcast Ninja Model Format](#2-nj--dreamcast-ninja-model-format)
3. [XJ — Blue Burst / PC Ninja Model Format](#3-xj--blue-burst--pc-ninja-model-format)
4. [GJ — GameCube Flipper Model Format](#4-gj--gamecube-flipper-model-format)
5. [BML — Multi-Model Archive](#5-bml--multi-model-archive)
6. [NJM — Ninja Motion / Animation Format](#6-njm--ninja-motion--animation-format)
7. [Stage Files (n.rel / d.rel)](#7-stage-files-nrel--drel)
8. [Texture Archives](#8-texture-archives)
9. [Platform & File Autodetection](#9-platform--file-autodetection)
10. [Texture-to-Model Association Strategies](#10-texture-to-model-association-strategies)

---

## 1. Shared Utilities & Conventions

### 1.1 Angle encoding

Rotation values throughout NJ/XJ/GJ/rel formats are stored as **signed 32-bit integers** representing 1/65536th of a full turn:

```
angle_radians = raw_int32 * (2π / 65536)
```

This gives a resolution of 65536 steps per revolution ("BAMS" — Binary Angle Measurement System, a common convention in Sega engines).

### 1.2 4×4 transform matrix construction ("DashMat4")

Every node/bone in every format builds its local transform the same way:

1. Start with identity.
2. **Rotate** — apply intrinsic-style rotations around X, then Y, then Z axes in turn (each axis's rotation is right-multiplied into the accumulated matrix in the order X, Y, Z).
3. **Translate** — right-multiply a translation matrix built from the node's position.
4. **Compose with parent** — right-multiply the parent's world matrix: `world = local * parent_world`.

Order matters: rotation is always applied before translation when building a node's local matrix, and the parent composition happens last. Reversing either order produces incorrect world-space positions for child nodes.

A node may skip rotation and/or translation entirely based on flag bits (see each format's node-flags table below) — a node with the "no rotation" flag set keeps an identity rotation for that node only; its parent's rotation is still inherited through composition.

Some DC-format bones use a **quaternion** instead of Euler angles (see §2.4) — the rotation matrix is built directly from the quaternion instead of from three axis rotations, but translation and parent-composition still happen in the same order.

Once a node's world matrix is known, vertex positions/normals are transformed by it:
- Position: `p' = p · R + T` (rotate then translate, i.e. full 4×4 transform)
- Normal: `n' = n · R` (rotation only, no translation)

### 1.3 Texture-archive discovery for standalone model files

When a model file (`.nj`/`.xj`/`.gj`) is imported directly (not from inside a BML), the texture archive is located by:

1. **Compound-extension match** — check for `model.nj.xvm`, `model.nj.gvm`, `model.nj.pvm` (and uppercase variants) beside the model file. This exists first in priority.
2. **Name-based search** — build a list of candidate name stems by:
   - the bare stem (`model`)
   - if the stem ends in `n`, `d`, `c`, or `r`, the stem with that letter stripped (`forest01n` → `forest01`)
   - from that stripped stem, also try stripping a trailing `_` + 1–3 digits (BB stage convention: `forest_01` → `forest`)
   - the full original stem as a final fallback
   
   For each candidate stem, try extensions in a platform-dependent priority order:
   - BB: `.xvm`, `.pvm`, `.gvm`
   - DC: `.pvm`, `.xvm`, `.gvm`
   - GC: `.gvm`, `.xvm`, `.pvm`
3. **Lone-file fallback** — if no name match is found, and the model's directory contains exactly one file with a given texture extension (checked in the same priority order), use that file.

### 1.4 Skybox files

A stage file named `xxxxxxN.rel` (case-insensitive trailing `n.rel`) may have a companion skybox model named `xxxxxxS.xj` / `.nj` / `.gj` (tried in that order) in the same directory, with an optional matching `.xvm`/`.pvm`/`.gvm` texture archive using the same stem. Detection is case-insensitive.

### 1.5 PRS decompression (SEGA LZSS variant)

BML archive entries are compressed with SEGA's PRS scheme, a byte-oriented LZ77 variant with a bit-flag stream interleaved into the byte stream:

- Maintain a "control byte": read one when the current bit supply is exhausted (8 bits per control byte, consumed LSB-first).
- **Bit = 1**: literal byte — copy one input byte directly to output.
- **Bit = 0**, then read next bit:
  - **Bit = 1** (long match): read 2 bytes `a`, `b`. Compute:
    ```
    offset = ((b << 8) | a) >> 3
    amount = a & 7
    if amount == 0:
        amount = next_byte() + 1
    else:
        amount = amount + 2
    start = current_output_length - 0x2000 + offset
    ```
  - **Bit = 0** (short match): read 2 more bits to form `amount_bits` (0–3), then one byte `offset`:
    ```
    amount = (bit << 1 | bit) + 2
    start  = current_output_length - 0x100 + offset
    ```
  - Copy `amount` bytes starting at `start` in the **output** buffer, one byte at a time (so overlapping copies replicate a repeating pattern, standard LZ77 behavior). Out-of-range `start` values are treated as producing a zero byte.
- Repeat until the input is exhausted.

---

## 2. NJ — Dreamcast Ninja Model Format

Used by PSOv1/v2 (Dreamcast) character models, props, and (in a variant) GameCube models packaged inside `.gj`/BML-adjacent contexts using the same chunk grammar. NJ is a **chunk-based** format wrapped in an outer container of top-level chunks.

### 2.1 Outer container

The file is a flat sequence of chunks, each:

```
u32 magic        (4-byte ASCII tag, stored as a little-endian uint when compared to MAGIC_* constants)
u32 chunk_length  (bytes of payload that follow)
u8  payload[chunk_length]
```

Read chunks until the stream is exhausted (last 4 bytes of the file are not part of a chunk header — the outer scan should stop 4 bytes before EOF). Relevant top-level magics:

| Magic (ASCII) | Hex (LE uint32) | Meaning |
|---|---|---|
| `NJTL` | `0x4C544A4E` | Texture name list |
| `NJCM` | `0x4D434A4E` | Geometry (bone/mesh chunk tree) |
| `NMDM` | `0x4D444D4E` | Animation data (see §6) |
| `POF0` | `0x30464F50` | Pointer-relocation table (see §2.5) |

Unknown top-level chunks are skipped using their own length field.

### 2.2 NJTL — texture name list

Payload layout:
```
u32 list_offset        (relative to start of NJTL payload)
u32 texture_count
```
Seek to `list_offset`; read `texture_count` entries, each:
```
u32 name_offset   (relative to start of NJTL payload)
u8  unknown[8]
```
For each `name_offset`, seek there and read a null-terminated ASCII string — this is the texture's original filename (e.g. `"CHAR01.PVR"`). The addon strips the extension and directory to get a clean display name, and maps texture list index → pre-loaded texture archive index **by array position** (index 0 in NJTL = texture index 0 in the loaded archive), not by name matching, for standalone NJ files.

### 2.3 NJCM — bone/mesh tree

The payload is read as a recursive tree of **bone nodes**, starting at payload offset 0 (the root). Each node:

```
u32 flags
u32 mesh_offset      (relative to NJCM payload start; 0 = no mesh)
f32 pos_x, pos_y, pos_z
i32 rot_x, rot_y, rot_z     (BAMS angle, see §1.1)   -- OR quaternion, see below
f32 scale_x, scale_y, scale_z
u32 child_offset     (relative to NJCM payload start; 0 = none)
u32 sibling_offset   (relative to NJCM payload start; 0 = none)
```
Total size: 52 bytes for Euler-rotation nodes.

**Flag bits** (bitmask on `flags`):
| Bit | Value | Meaning |
|---|---|---|
| 0x01 | | Skip translation — node's position field is ignored (treated as origin) |
| 0x02 | | Skip rotation — node's rotation field is ignored (treated as identity) |
| 0x400 | | Rotation is stored as a **quaternion** instead of Euler angles (see below) |

**Quaternion variant** (flag `0x400` set): the node is 56 bytes — same layout, but the three `i32` rotation fields are reinterpreted as `f32 qx, qy, qz`, and one additional `f32 qw` field is appended after `sibling_offset`:
```
u32 flags
u32 mesh_offset
f32 pos_x, pos_y, pos_z
f32 qx, qy, qz              (quaternion x/y/z, as floats — NOT BAMS ints)
f32 scale_x, scale_y, scale_z
u32 child_offset
u32 sibling_offset
f32 qw                      (quaternion w, appended field)
```
Build the rotation matrix directly from the quaternion (standard quaternion-to-matrix formula) rather than from three axis rotations.

**Traversal**: depth-first. At each node:
1. Build the node's local matrix (rotate unless flag 0x02, translate unless flag 0x01), then compose with the parent's world matrix (see §1.2).
2. Bounds-check `mesh_offset`, `child_offset`, `sibling_offset` against the NJCM payload size — if **any** exceeds the payload size, abandon this node's mesh **and** its entire subtree (do not recurse into children or siblings). This matches an idiosyncrasy of the reference tooling and is required to avoid corrupting output on certain malformed/corrupted files.
3. If `mesh_offset != 0`, seek there and read the mesh chunk tree (§2.4).
4. If `child_offset != 0`, seek there and recurse with this node as parent.
5. If `sibling_offset != 0`, seek there and recurse with the **same parent** as this node (siblings share a parent, not each other).

Record nodes in depth-first order for building a skeleton/armature; each mesh chunk's vertices are tagged with the index of the node that was most recently entered (the "current bone index"), enabling per-vertex bone assignment for skinned meshes.

### 2.4 Mesh sub-chunk (pointed to by `mesh_offset`)

```
u32 vertex_chunk_offset   (0 = none)
u32 poly_chunk_offset     (0 = none)
f32 center_x, center_y, center_z
f32 radius
```
(20 bytes.) If `vertex_chunk_offset != 0`, seek there and read the **chunk-list grammar** below; likewise for `poly_chunk_offset`. Both chunk lists use the same grammar — they're just conventionally split into a vertex-declaration stream and a polygon/material stream.

### 2.5 NJD chunk-list grammar

This is the heart of the NJ format: a self-terminating sequence of typed chunks, each carrying a **channel** (`ch`, 0–255) and a **chunk-flags** byte (`cf`, 0–255). Little-endian files read `ch` and `cf` as two separate bytes (`ch` first). GameCube (big-endian) files use a different, ambiguous encoding — see §2.5.1.

Chunk type ranges:

| Range | Name | Purpose |
|---|---|---|
| 0 | `NJD_CN` (null) | No-op, continue |
| 255 | `NJD_CE` (end) | Terminate this chunk list (or jump, see "DP/CP" below) |
| 1–5 | `BITS` | Store-position / dispatch-position markers |
| 8–9 | `TINY` | (Reserved / unused by addon) |
| 17–23 | `MATERIAL` | Material property chunks |
| 32–50 | `VERTEX` | Vertex declaration chunks |
| 56–58 | `VOLUME` | Legacy strip format ("volume" chunks) |
| 64–75 | `STRIP` | Triangle-strip chunks (current format) |

**BITS chunks (1–5)**: `NJD_CB_CP` (store position, cf = a 0–255 slot index) records the current stream position into slot `cf` and **halts** chunk-list reading (acts as a "yield"/checkpoint used for animation frame boundaries in the original engine — for a static-mesh parser it simply means: stop reading this chunk list here). `NJD_CB_DP` (dispatch/jump) seeks to the stored position for slot `cf` and continues reading, remembering the return address so `NJD_CE` (end) jumps back instead of terminating for real.

**VERTEX chunks (32–50)**: header is
```
u16 chunk_length_words     (length of everything after this field, in 32-bit words, plus 1)
u16 vertex_base_offset     (offset into the addressable vertex "stack"/index space this batch starts at)
u16 vertex_count
```
followed by `vertex_count` vertex records. The chunk **sub-type** (32–50) determines which optional fields each vertex record has:

| Sub-type name | Value | Has normal? | Has color? | Notes |
|---|---|---|---|---|
| `NJD_CV_SH` | 32 | no | no | Position + 1 padding float (SH4 `w=1.0`) |
| `NJD_CV_VN_SH` | 33 | yes | no | Position+pad, normal+pad |
| `NJD_CV` | 34 | no | no | Bare position |
| `NJD_CV_D8` | 35 | no | yes | Position + packed BGRA color |
| `NJD_CV_UF` | 36 | no | no | |
| `NJD_CV_NF` | 37 | no | no | Special: see below |
| `NJD_CV_S5` | 38 | no | no | |
| `NJD_CV_S4` | 39 | no | no | |
| `NJD_CV_IN` | 40 | no | no | |
| `NJD_CV_VN` | 41 | yes | no | Position + normal (both float triples) |
| `NJD_CV_VN_D8` | 42 | yes | yes | Position + normal + BGRA color |
| `NJD_CV_VN_UF` | 43 | yes | no | |
| `NJD_CV_VN_NF` | 44 | yes | no | Special: see below |
| `NJD_CV_VN_S5` | 45 | yes | no | |
| `NJD_CV_VN_S4` | 46 | yes | no | |
| `NJD_CV_VN_IN` | 47 | yes | no | |
| `NJD_CV_VNX` | 48 | yes (packed) | no | Position + packed 10:10:10 normal |
| `NJD_CV_VNX_D8` | 49 | yes (packed) | yes | Position + packed normal + BGRA color |
| `NJD_CV_VNX_UF` | 50 | yes (packed) | no | |

Common per-vertex record layout, in read order:
1. `f32 x, y, z` — position (transformed by the current world matrix)
2. If sub-type is one of the `_SH` variants, one extra `f32` pad follows position (ignored, historically a homogeneous `w=1.0`).
3. Normal, if the sub-type has one:
   - **Packed** (`VNX` variants): one `u32`. Unpack as three signed 10-bit fields:
     ```
     nx = ((packed >> 20) & 0x3FF) / 511.0 - 1.0
     ny = ((packed >> 10) & 0x3FF) / 511.0 - 1.0
     nz = ( packed        & 0x3FF) / 511.0 - 1.0
     ```
   - **Float** (`VN` variants, not `VNX`): `f32 nx, ny, nz`, plus one more pad float if `_SH`.
4. Color, if the sub-type has one (`_D8` variants): 4 bytes in **BGRA** order, normalized 0–1 (i.e. read blue, green, red, alpha, in that order, each divided by 255).
5. **`NJD_CV_NF` / `NJD_CV_VN_NF` only**: instead of storing at index `vertex_base_offset + i` in the vertex stack, these have a 4-byte trailer of `i16 nofs` (a *relative* vertex-stack index offset) and `i16 padding`, and the vertex is instead stored at stack index `vertex_base_offset + nofs`. This lets an animated/skinned vertex be aliased into another bone's local vertex list while keeping the position in the **current node's local space** — critical: the addon stores these vertices in **local space** (relative to the current bone, pre-transform) rather than world space, and applies the bone's matrix at render/pose time. Storing world-space coordinates here (as if copying from a sibling bone's already-transformed vertex) produces visibly distorted limbs on skinned Blue-Burst characters, because a mirrored/rotated sibling bone's world position is wrong for the target bone.

Per-vertex byte sizes (used to validate ambiguous chunk headers on GC — see §2.5.1):

| Sub-type | Bytes/vertex |
|---|---|
| 32 (SH) | 16 |
| 33 (VN_SH) | 32 |
| 34 (plain) | 12 |
| 35 (D8) | 16 |
| 36–40 (UF/NF/S5/S4/IN) | 12 |
| 41 (VN) | 24 |
| 42 (VN_D8) | 28 |
| 43 (VN_UF) | 24 |
| 44 (VN_NF) | 28 |
| 45–47 (VN_S5/S4/IN) | 24 |
| 48 (VNX) | 16 |
| 49 (VNX_D8) | 20 |
| 50 (VNX_UF) | 16 |

**MATERIAL chunks (17–23)**: header is `u16 chunk_length_words` followed by up to 3 optional color fields, gated by bits of the chunk type value itself (`ch & 0x01` = has diffuse, `ch & 0x02` = has ambient, `ch & 0x04` = has specular):
- Diffuse: 4 bytes BGRA, normalized.
- Ambient: 4 bytes (B, G, R, then 1 unused byte), alpha forced to 1.0.
- Specular: 4 bytes (B, G, R, then 1 unused byte), alpha forced to 1.0.

The chunk-flags byte `cf` additionally encodes a blend mode: `src = cf & 0x07`, `dst = (cf >> 3) & 0x07`. `src==1, dst==4` → additive blending ("ONE"/"ONE"); `src==5, dst==4` → normal alpha blending (no special flag needed).

**TINY chunks (8–9)**: header is `u16 body`. The low 13 bits of `body` (`body & 0x1FFF`) are a **texture index** into the loaded texture archive, stored as the active material's current texture.

**VOLUME chunks (56–58)** (legacy strip format): header `u16 chunk_length_words`, then `u16 body` where the low 14 bits (`body & 0x3FFF`) give a strip count. For each strip: a signed `i16` whose absolute value is the strip's point count and whose sign indicates initial winding (negative = clockwise start), followed by that many `u16` vertex-stack indices (no UV data in this legacy format). Strips are converted to a triangle list, alternating winding order every other triangle within the strip (classic triangle-strip decoding).

**STRIP chunks (64–75)** (current strip format): header `u16 chunk_length_words`, then `u16 body`. Bit `0x3FFF` of `body` gives the strip count; bits above that (`body >> 14`) give a "user data" word-count per point beyond index+UV, to be skipped. The chunk-flags byte's `0x10` bit means "double-sided": emit both triangle windings for every triangle.

For each strip: `i16` (sign = winding, abs value = point count), then that many points, each:
```
u16 vertex_stack_index
[optional UV, depending on chunk sub-type]
```
UV sub-types:
- `NJD_CS_UVN` (65): `i16 u, i16 v`, each divided by 255.0 (8.8-ish fixed point). V is flipped (`1.0 - v`) for Blender's bottom-up UV convention.
- `NJD_CS_UVH` (66): `i16 u, i16 v`, each divided by 1023.0 (higher precision fixed point). Same V flip.
- All other STRIP sub-types: no UV per point.

After the (optional) UV, if this is not one of the first two points in the strip and `user_offset` (from `body >> 14`) is non-zero, skip `user_offset * 2` bytes of unused per-point user data.

Strip → triangle conversion: alternating winding, same as VOLUME chunks. If double-sided, emit the mirrored triangle as well.

**End-of-list / jump handling**: encountering `NJD_CE` while a "jump-to" address is pending (set by a `DP` bits-chunk) seeks there and continues instead of stopping.

#### 2.5.1 GameCube big-endian chunk-header disambiguation

On GC, NJ chunk **payloads** are stored big-endian even though the outer top-level chunk headers (magic + length, §2.1) stay little-endian. Within the chunk-list grammar, GC's header encoding is genuinely ambiguous and must be disambiguated by a lookahead/validation heuristic:

- For **no-length chunk types** (NULL/END, BITS, TINY, STRIP, VOLUME, MATERIAL), the on-disk layout is a **single** big-endian `u16` word whose low byte is `ch` and high byte is `cf`.
- For **VERTEX chunks**, the on-disk layout is **two** big-endian `u16` words: first a chunk-length word, then the `ch`/`cf` word (low byte `ch`, high byte `cf`).

The problem: a vertex chunk's length word's low byte can, for small vertex counts, fall inside the numeric ranges used by no-length chunk types (TINY 8–9, BITS 1–5, STRIP 64–75, VOLUME 56–58, MATERIAL 17–23, or the generic range 0x10–0x1F). A naive single-word read would then misinterpret that length word as a spurious `ch`/`cf` pair.

Disambiguation algorithm:
1. Read the first big-endian `u16` (`word0`). Extract `ch_cand = word0 & 0xFF`.
2. If `ch_cand` is 0 or 0xFF (NULL/END), it's unambiguous — treat as a no-length chunk.
3. Otherwise, if `ch_cand` falls in one of the no-length ranges, **peek** the next `u16` (`word1_peek`) without consuming it yet. Extract `vc_ch = word1_peek & 0xFF`.
4. If `vc_ch` is **not** in the VERTEX range (32–50), `word0` really was a no-length `ch`/`cf` pair — restore the stream position to just after `word0` and proceed as a no-length chunk using `ch = ch_cand`, `cf = (word0 >> 8) & 0xFF`.
5. If `vc_ch` **is** in the VERTEX range, this might be a real vertex chunk whose length word happened to alias a no-length type. Validate with two checks before committing:
   - `body_bytes = word0 * 4 - 4` must be a non-negative multiple of the known per-vertex byte size for `vc_ch` (table in §2.4) — this yields an *expected* vertex count.
   - Read the actual `u16` vertex-count field that would follow (per the VERTEX chunk header layout) and compare it to the expected count. They must match exactly.
   - If both checks pass, commit to interpreting this as a vertex chunk (`ch = vc_ch`, `cf` from the high byte of `word1_peek`), and leave the stream positioned at the vertex-count field so the vertex-chunk reader can proceed normally.
   - If either check fails, it was a false positive — restore to just after `word0` and treat it as a no-length chunk instead (same as step 4).
6. If `ch_cand` is **not** in a no-length range (i.e. it's in the VERTEX range itself, 32–50), then `word0` was unambiguously the vertex chunk's length word — read the following `u16` for `ch`/`cf` directly, no peeking needed.

Within a confirmed GC vertex chunk, the vertex-count and vertex-base-offset fields are swapped relative to the little-endian layout: GC reads `u16 vertex_count` **then** `u16 vertex_base_offset` (the opposite order from LE files, where `vofs` precedes `vcount`). GC vertex, material, and strip/volume chunk length fields (once identified) are always big-endian and are otherwise consumed the same way as their LE counterparts (read-and-discard, since the length is derivable from content).

The `NJD_CV_VN_NF` special 4-byte trailer (§2.4) also differs on GC: because the whole 32-bit word containing `[nofs:i16, padding:i16]` is byte-order-flipped as a unit, the two 16-bit halves swap position — GC layout is `[padding, nofs]` instead of little-endian's `[nofs, padding]`. Read padding first, then `nofs`, on GC.

### 2.6 POF0 — pointer relocation table

Some NJ files (observed in certain GameCube boss models) are packaged with a non-zero **serialization base**: every pointer field inside the NJCM payload has a constant `B` added to it at build time, and the file ships a POF0 chunk that lists which 4-byte words in the payload are pointers so a loader can subtract `B` back out.

**POF0 payload encoding** — a variable-length list of byte offsets (into the target payload), delta-encoded:
```
loop:
  read byte b
  if b == 0: stop (end of list)
  top2 = b >> 6
  if top2 == 0:  delta = b * 4                                    (small jump)
  if top2 == 1:  read byte b2; delta = ((b & 0x3F) << 8  | b2) * 4 (medium jump)
  if top2 == 2:  read bytes b2,b3; delta = ((b & 0x3F) << 16 | b2 << 8 | b3) * 4  (large jump)
  if top2 == 3:  invalid / unused, stop
  current_offset += delta
  emit current_offset
```

**Applying relocation**:
1. Collect `(offset, raw_pointer_value)` for every offset in the POF0 list, read as a `u32` from the NJCM payload at that offset (respecting the file's overall endianness).
2. If every non-zero pointer value is already less than the payload size, no relocation is needed — return unchanged.
3. Otherwise, split non-zero values into "invalid" (≥ payload size) and "valid" (< payload size) groups. The invalid group's `min`/`max` bound the search for `B`:
   - `B` must satisfy `max_invalid - B < payload_size` (keeps the largest invalid pointer in range) and `min_invalid - B >= 4` (keeps the smallest invalid pointer positive and past the root node).
   - If any already-valid pointers exist, `B` must additionally keep them positive after subtraction.
4. Search candidate `B` values by iterating candidate "true offsets" for the smallest invalid pointer (i.e. `B = min_invalid - true_min`, for `true_min` stepping by 4 from 4 up to ~4096 or the payload size). For each candidate `B`:
   - Verify all invalid pointers land strictly inside `(0, payload_size)` after subtracting `B`.
   - Verify all originally-valid pointers stay positive after subtracting `B`.
   - **Structural validation**: the root bone always lives at NJCM offset 0, and its `child_offset`/`sibling_offset` fields (bytes 44 and 48) are known to point at other bone nodes — a bone node always starts with a `flags` field that is a small, sane value (`<= 0x3FFF`). Check that the *relocated* targets of these two known-bone pointers (or, if unavailable, the first several other invalid pointers) start with a plausible small flags value.
   - Accept the first `B` that passes all checks.
5. If no candidate passes the structural check, fall back to accepting the first `B` that merely satisfies the range constraints (without the bone-flags check) — but do **not** actually apply this weaker fallback to the payload (it's not trusted enough); only apply a structurally-validated `B`.
6. If a validated `B` was found, subtract it from every non-zero pointer at its recorded offset, in place, and return the patched payload. Otherwise return the payload unmodified.

A file may contain two POF0 chunks: a small one before the NJCM (relocating NJTL string-list pointers — never apply this to NJCM) and a larger one after it (relocating NJCM's own bone/mesh pointers). Always prefer a POF0 chunk located **after** the NJCM chunk ends; only use a before-NJCM POF0 as a last resort, and never apply a before-NJCM POF0 to the NJCM payload.

---

## 3. XJ — Blue Burst / PC Ninja Model Format

XJ shares the same outer-container grammar as NJ (`NJTL`/`NJCM`/`NMDM` magics, same chunk types) but with a materially different **mesh sub-chunk layout** — it does not use the NJD chunk-list grammar (§2.5) at all for its polygon data. Instead:

### 3.1 Bone tree

Identical 52-byte node layout to NJ's Euler-rotation case (§2.3) — flags, mesh_offset, position, BAMS rotation, scale, child_offset, sibling_offset. XJ does not use the NJ quaternion-bone variant.

### 3.2 Mesh sub-chunk

```
u32 flags
u32 vertex_info_list_offset
u32 vertex_info_count
u32 triangle_strip_list_a_offset
u32 triangle_strip_a_count
u32 triangle_strip_list_b_offset
u32 triangle_strip_b_count
f32 center_x, center_y, center_z
f32 radius
```
(36 bytes.)

### 3.3 Vertex info list

If `vertex_info_list_offset != 0`, seek there and read `vertex_info_count` entries, each 16 bytes:
```
u16 vtype
u16 unknown
u32 vertex_data_offset
u32 vertex_size          (unused directly — recomputed from vtype)
u32 vertex_count
```
`vtype` bit flags: `0x01` = has UV, `0x02` = has normal, `0x04` = has vertex color.

For each of `vertex_count` vertices at `vertex_data_offset`, in order:
1. `f32 x, y, z` — transformed by the current node's world matrix.
2. If `vtype & 0x02`: `f32 nx, ny, nz` — transformed by the world matrix's rotation-only part.
3. If `vtype & 0x04`: 4 bytes in **RGBA** order (note: NOT BGRA — this differs from NJ/DC), normalized 0–1.
4. If `vtype & 0x01`: `f32 u, f32 v`; stored V-flipped (`1.0 - v`).

Vertices are indexed by their position in this pass (0-based) into a per-mesh vertex stack, reset for each mesh.

### 3.4 Triangle strip lists (A/B)

If a strip-list count is non-zero, seek to its offset and read that many strip-info records, each 20 bytes:
```
u32 material_property_list_offset
u32 material_property_list_size
u32 index_list_offset
u32 index_count
u32 unknown
```
For each strip-info record: seek to `material_property_list_offset` and read `material_property_list_size` material property blocks (§3.5); then seek to `index_list_offset` and read `index_count` signed 16-bit vertex-stack indices.

Strip decoding for XJ is **not** alternating-winding like NJ — instead, every consecutive index triple `(i, i+1, i+2)` (skipping degenerate triples where any two of the three are equal) emits **two** triangles: `(i, i+1, i+2)` and `(i, i+2, i+1)` — i.e. both winding directions, producing double-sided geometry regardless of any "double-sided" material flag.

### 3.5 Material property blocks

Each block starts with a `u32 mat_type` tag:

| `mat_type` | Meaning | Payload |
|---|---|---|
| 2 | Blend mode | `u32 dst, u32 src`, then 4 bytes ignored. `src==1,dst==4` → additive; `src==5,dst==4` → normal alpha. |
| 3 | Texture index | `u32 tex_id`, then 8 bytes ignored. Sets the active material's texture index directly (no masking). |
| 5 | Diffuse color | 4 bytes **RGBA** (not BGRA), normalized, then 8 bytes ignored. |
| other | Unknown | 12 bytes ignored. |

Materials are de-duplicated by a composite key of (diffuse color, texture index, blend src, blend dst, has-vertex-colors) so repeated identical strips share one Blender material.

---

## 4. GJ — GameCube Flipper Model Format

Used for GameCube PSO Episode I & II (and, for stage geometry, Episode III — see §7.3). The outer file uses the same `NJCM`/`NJTL`-style container idea but with GC-specific magic values, and the payload is **big-endian** throughout (the outer chunk length/magic fields are read little-endian for the initial magic scan, but chunk contents are big-endian once entered).

### 4.1 Outer container

```
u32 magic          (compared against GJ magics below, LE)
u32 chunk_length
u8  payload[chunk_length]   (big-endian content)
```

| Magic (ASCII) | Hex (LE uint32) | Meaning |
|---|---|---|
| `GJTL` | `0x4C544A47` | Texture name list |
| `GJCM` | `0x4D434A47` | Geometry (node tree) |

Only the **first** `GJCM` chunk found is treated as the model's geometry; parsing stops after it.

### 4.2 GJTL — texture name list

```
u32 list_offset       (BE, relative to GJTL payload start)
u32 count             (BE)
```
Seek to `list_offset`; read `count` entries, each 12 bytes: `u32 name_offset` (BE) followed by 8 bytes ignored. For each `name_offset`, seek and read a null-terminated ASCII string.

**Texture remapping**: if a texture archive has already been loaded into the importer (`self.textures`) *before* GJTL is parsed, and that archive contains genuine human-readable names (i.e. not generic placeholder names like `texture_000`), remap the texture list so that GJTL name index 0 refers to whichever pre-loaded texture has a matching name (case-insensitive), index 1 to the next matching name, and so on. If no match is found for a given GJTL name, substitute an 8×8 blank placeholder texture. This remapping is skipped (leaving textures in raw archive order) when the pre-loaded archive lacks real names.

This remapping matters because a single texture archive can be shared across many models (e.g. one GVM file used by 20+ props in a BML archive) with a different texture ordering than any individual model's own GJTL list expects — using the model's local GJTL-index-based ordering to reindex into the shared archive's global ordering, by name, produces correct texture assignment. See §10 for the full picture, including the case where GJTL is entirely absent.

### 4.3 GJCM — node tree

Each node, read big-endian:
```
u32 flags
u32 mesh_offset
f32 pos_x, pos_y, pos_z
i32 rot_x, rot_y, rot_z    (BAMS angle)
f32 scale_x, scale_y, scale_z
u32 child_offset
u32 sibling_offset
```
(52 bytes; same conceptual layout as NJ's Euler node, but no quaternion variant exists for GJ.) Flag bits `0x01` (skip translation) and `0x02` (skip rotation) behave identically to NJ.

Traversal is depth-first, identical in structure to NJ's (§2.3): validate `mesh_offset`/`child_offset`/`sibling_offset` are within the payload bounds (using `<=` for GJ, versus strict `<` used elsewhere — a minor format quirk), read the mesh if present, then recurse into child (with this node as parent) and sibling (with this node's *original* parent, not this node).

### 4.4 GJ mesh sub-chunk

```
u32 vertex_property_list_offset
u32 unused                        (always zero)
u32 strip_list_offset
u32 alpha_strip_list_offset
i16 strip_count
i16 alpha_strip_count
f32 center_x, center_y, center_z
f32 radius
```
(32 bytes.) If `vertex_property_list_offset != 0`, read the vertex property list (§4.5). Then, if non-zero, read `strip_count` strips from `strip_list_offset` (opaque) and `alpha_strip_count` strips from `alpha_strip_list_offset` (alpha-blended) — both go through the same strip reader (§4.6); the alpha/non-alpha distinction is tracked but does not change parsing logic.

### 4.5 Vertex property list ("GX attribute arrays")

A self-terminating list of attribute descriptors:
```
loop:
  u8 type       (0xFF = end of list)
  u8 size
  u16 count
  u32 unknown
  u32 offset     (into the GJCM payload)
  u32 length
```
`type` values: `1` = position, `2` = normal, `3` = color, `5` = UV. (Any other type's `count` entries are skipped using `size` bytes each.)

For each attribute, seek to `offset` and read `count` entries:
- **Position** (type 1): `f32 x, y, z` (BE), transformed by the current node's world matrix.
- **Normal** (type 2): `f32 x, y, z` (BE), transformed by the world matrix's rotation part.
- **Color** (type 3): a single big-endian `u32` packing all four channels in **RGBA** byte order (i.e. reading the u32 and shifting: bits 31–24 = R, 23–16 = G, 15–8 = B, 7–0 = A), each normalized 0–1.
- **UV** (type 5): `i16 u, i16 v` (BE), each divided by 255.0; V flipped (`1.0 - v`).

These become flat arrays (`pos[]`, `norm[]`, `color[]`, `uv[]`) indexed by GX vertex indices in the strip data (§4.6), not by a per-mesh running counter — i.e. the strip's index values directly index into these attribute arrays.

### 4.6 Strip list & material properties

Each strip-list entry (before reading strips) is a 16-byte polygon-group record:
```
u32 material_property_offset
u32 material_property_count
u32 strip_data_offset
u32 strip_data_byte_length
```
For each polygon group: seek to `material_property_offset` and read `material_property_count` GC material properties (§4.7); then seek to `strip_data_offset` and read exactly `strip_data_byte_length` bytes of GX primitive/index data (§4.8).

### 4.7 GC material properties

Each property is 8 bytes:
```
u8  type
u8  padding[3]
u32 value        (BE)
```
- `type == 0x01`: `value` is stored as the current **face-flags** word — this word's bits later determine the GX vertex-index attribute widths used when decoding strip indices (§4.8).
- `type == 0x08`: `value & 0x1FFF` (mask to 13 bits) is the texture index.
  - If a GJTL texture-name list was present for this model, validate the index against `len(texNames)`; out-of-range → texture index -1 (no texture).
  - If **no** GJTL was present (this model's textures come entirely from an external/shared archive with no local name list — the common case for BML-embedded GameCube props), validate instead against the size of the loaded texture archive (`len(textures)`) — because in that case, `value & 0x1FFF` is an **absolute index directly into the shared texture archive**, not a local GJTL-relative index. Getting this fallback wrong (validating against an empty `texNames` list) makes every texture index invalid and produces fully texture-less models.

### 4.8 GX primitive/index stream (`face_flags`-driven)

Before decoding, derive the per-attribute index width from the **face_flags** word (set by the most recent type-0x01 material property, §4.7). Each of the four possible vertex attributes (position, normal, color, UV) has its own **independent** 2-bit "is this attribute present, and is its index 8-bit or 16-bit" pair of flag bits:

| Attribute | "Present" bit | "16-bit index" bit |
|---|---|---|
| Position | `0x0008` | `0x0004` |
| Normal | `0x0020` | `0x0010` |
| Color | `0x0080` | `0x0040` |
| UV | `0x0800` | `0x0400` |

For each attribute whose "present" bit is set, if its corresponding "16-bit" bit is also set, that attribute's per-vertex index in the primitive stream is a `u16`; otherwise it's a `u8`. **Each attribute's index width must be determined independently from its own bit pair** — do not assume all present attributes share a single global index width. An earlier, buggy implementation read a single shared index-size flag for the whole vertex and produced corrupted geometry (misinterleaved position/normal/color/UV indices) whenever a model mixed 8-bit and 16-bit attribute indices within the same vertex, which does occur in real files.

Build an ordered list of `(attribute_name, index_byte_width)` pairs for only the present attributes, in the fixed order position, normal, color, UV — this defines the per-vertex read order in the primitive stream.

Then, read primitives until `strip_data_byte_length` bytes are consumed:
```
loop until byte budget exhausted:
  u8 primitive_type
  if primitive_type == 0x00: continue   (padding byte)
  if primitive_type not in (0x90, 0x98): abort — malformed stream
  u16 vertex_count
  for vertex_count vertices:
      for each (attribute_name, index_width) in the present-attribute list:
          index = read u16 or u8 per index_width
          look up index in that attribute's flat array (pos[]/norm[]/color[]/uv[]) from §4.5;
          out-of-range indices are simply omitted for that vertex (not an error)
```
- `primitive_type == 0x98` is a **triangle fan**: vertex 0 is the fan's hub; triangles are `(0, i+1, i+2)` style alternating winding — specifically for `k` in `0..count-3`: alternate between `(k, k+2, k+1)` and `(k+1, k+2, k)` as the winding flips each step.
- `primitive_type == 0x90` is a **triangle list**: every consecutive group of 3 vertices is one triangle, taken directly with a fixed winding `(3i, 3i+2, 3i+1)`.

Each decoded vertex accumulates into a running mesh vertex list (offset by an accumulator that increases per primitive so multiple primitives within one strip don't collide indices), and triangle indices are collected into a triangle list referencing that running vertex list.

Materials are de-duplicated the same way as NJ/XJ (§3.5's key structure): (diffuse, texIndex, blendSrc, blendDst, has_vertex_colors).

### 4.9 GJ stage geometry (`parse_stage`)

GameCube stage files (`n.rel`) reuse the exact same node/mesh/strip/material readers described above (§4.3–§4.8), but are entered through a different top-level routine because they have no `GJCM`/`GJTL` wrapper — see §7.3 for the stage-specific container format.

---

## 5. BML — Multi-Model Archive

BML packages multiple models (props with variants, LOD levels), their animations, and optionally embedded per-model texture archives into one file. Used on both DC and GC.

### 5.1 Header & endianness detection

Read a `u32` count field at **byte offset 4** in both byte orders. Whichever interpretation gives a small positive value (`0 < count <= 2000`) is the correct endianness; GC files are big-endian, DC/PC files little-endian. If both interpretations look plausible, prefer big-endian (GC files take priority in this ambiguous case).

### 5.2 Entry table

Starting at fixed offset **0x40**, read `count` entries. Each table slot occupies **0x40 (64) bytes total**, but only the first `0x34` (52) bytes are meaningful fields — the remaining 12 bytes are padding, skipped after reading the fields:

```
char name[32]              (ASCII, null/space/whitespace-padded)
u32  compressed_size
u32  unused
u32  decompressed_size
u32  paired_texture_compressed_size     ("pvm_comp")
u32  paired_texture_decompressed_size   ("pvm_decomp")
[12 bytes padding]
```
Trim the name of trailing spaces/tabs/CR/LF/NUL.

If `paired_texture_compressed_size != 0`, this model entry has an **embedded texture archive** immediately following it in the compressed data stream. Synthesize a second table entry for it: name = the model's name with its extension replaced by `.gvm` (if the archive is big-endian/GC) or `.pvm` (if little-endian/DC), sizes taken from the `paired_texture_*` fields.

### 5.3 Compressed data region

Compressed payloads begin at the next **0x800-byte-aligned** offset after the entry table (round the post-table stream position up to the next multiple of 0x800; if it's already aligned, do not advance further).

For each table entry in order: skip any zero-padding bytes at the current position (entries are 0-padded between each other, not necessarily to any particular alignment), then read exactly `compressed_size` bytes and PRS-decompress them (§1.5) to get that entry's raw data. Advance the read cursor by `compressed_size` (not `decompressed_size`) before looking for the next entry's padding+data.

### 5.4 Entry classification (post-decompression)

Every decompressed entry is classified by its filename extension:
- `.nj`, `.gj`, `.xj` — a model. Each model entry is paired with a texture, resolved with this priority:
  1. **Immediately following entry** in the archive, if its extension is `.pvm` or `.gvm` (the standard synthesized-pair layout from §5.2).
  2. **Compound name match**: an entry anywhere in the archive whose name equals `model_name + '.xvm'` / `'.gvm'` / `'.pvm'` (case-insensitive) — for models whose paired texture isn't the very next entry.
- `.njm` — an animation (see §6); collected separately from the model/texture pairs.
- Anything else is silently ignored.

### 5.5 Texture resolution and sharing, per model

At import time, four sources are tried in order for a given model's textures (see §10 for full detail on why so many fallbacks exist):
1. The BML-embedded texture pair (§5.4, priority 1 or 2).
2. A per-model sidecar file beside the BML on disk (`model_stem.gvm`/`.GVM`/`.pvm`/`.PVM`/`.xvm`/`.XVM`, tried in that order).
3. A BML-level shared sidecar file — same base name as the `.bml` file itself, same extension search order — loaded once and reused for every model in the archive that has no more specific texture source.
4. Reuse of the **previous model's** resolved textures (LOD/variant models, e.g. `lo_`/`hi_`/`sd_` prefixed names, that appear directly after their "parent" model in archive order and carry no texture reference of their own).

Textures loaded once (by name+dimensions) are reused across all models in the archive rather than re-decoded/re-uploaded, to avoid producing dozens of duplicate copies of the same texture in the target scene when many models share one archive.

---

## 6. NJM — Ninja Motion / Animation Format

Two container variants exist; both wrap the same motion-header/keyframe-table structure.

### 6.1 Container variants

**v2 / DC / GC variant**: file begins with the `NMDM` magic (`0x4D444D4E`, read little-endian for the initial scan), followed by `u32 chunk_size`, then the motion header begins immediately.

**BB player-format variant** (no `NMDM` magic): the file instead has a footer-based indirection chain, always little-endian:
```
seek to (file_length - 16); read u32 offset1
seek to offset1;             read u32 action_offset
seek to (action_offset + 4); read u32 motion_start   <- this is the actual motion header position
```

### 6.2 Endianness re-detection

Regardless of container variant, once `motion_start` is known, read the 4 bytes there as both a little-endian and a big-endian signed `i32` ("`m_data_table_rel`"). Whichever interpretation yields a value in the plausible range `4 <= v <= 4096` is the correct byte order for the rest of the file (GC animations are big-endian; everything else little-endian). If neither interpretation is plausible, the file cannot be parsed.

### 6.3 Motion header

At `motion_start`, in the now-known byte order:
```
i32 m_data_table_rel     (byte offset of the per-bone offset/count table, relative to motion_start)
i32 frame_count
u16 motion_type          (bitmask of which channels are present)
u16 inp_fn                ( low byte = interpolation type: 0 = Linear, 1 = Spline/Bezier )
```

`motion_type` bit flags (channel presence, in this bit order — this also determines column order in the per-bone table):
| Bit | Channel |
|---|---|
| `0x0001` | Position |
| `0x0002` | Euler rotation |
| `0x2000` | Quaternion rotation |
| `0x0004` | Scale |

Channel order for table layout purposes is always: position, euler, quaternion, scale (skipping any that aren't present) — **not** the bit-value order above; e.g. quaternion (bit `0x2000`) sorts before scale (bit `0x0004`) in the table despite the bit value being larger.

If no channels are present, the file is not usable as an animation.

### 6.4 Per-bone offset/count table

Table location: `motion_start + m_data_table_rel`. Each present channel contributes one `(offset, count)` pair of `i32`s per bone, but the table is stored **struct-of-arrays**, not interleaved: for a bone entry, all N channel offsets come first, then all N channel counts:
```
per-bone entry (N = number of present channels):
  i32 offset[N]     (each relative to motion_start)
  i32 count[N]      (keyframe count for that channel)
```
So each bone's table entry is `N * 8` bytes.

**The table has no stored element (bone) count** — its length must be inferred. Do this by scanning up to 512 candidate bone-table entries and, for every `(offset, count)` pair with `count > 0`, checking that `offset >= m_data_table_rel + (bone_index + 1) * bytes_per_bone` (i.e. the keyframe data this entry points to must lie *after* the table itself, past this and all preceding entries) — this excludes spurious small values that would otherwise be misread as valid table offsets once the scan runs past the table's true end into keyframe data. Track the **minimum** valid offset found across the whole scan; the number of bones is:
```
element_count = (min_valid_offset - m_data_table_rel) / bytes_per_bone
```

### 6.5 Keyframe data, per channel

For each bone, for each present channel, seek to `motion_start + offset` and read `count` keyframes:

- **Position**: 16 bytes/keyframe — `i32 frame, f32 x, f32 y, f32 z`.
- **Euler rotation**: two possible encodings, auto-detected per-track:
  - **Compact** (8 bytes/keyframe): `u16 frame, u16 rx, u16 ry, u16 rz` — used when frame numbers fit in 16 bits.
  - **Wide** (16 bytes/keyframe): `i32 frame, i32 rx, i32 ry, i32 rz`.
  - Detection: read the first keyframe's frame value as a `u16`. If it's `>= frame_count`, assume wide encoding. Otherwise, tentatively assume compact and verify by reading up to 7 more frame-number fields at 8-byte stride, checking they are monotonically non-decreasing and each less than the previous check's threshold; any violation flips the assumption to wide.
  - Rotation components are BAMS angles (§1.1): multiply by `2π/65536`.
- **Quaternion rotation**: 20 bytes/keyframe — `i32 frame, f32 w, f32 x, f32 y, f32 z` (note: W is stored **before** X/Y/Z).
- **Scale**: 16 bytes/keyframe — `i32 frame, f32 x, f32 y, f32 z`.

Frame numbers in output keyframes are typically incremented by one when placed into a target animation timeline (frame 0 in the source data → frame 1 on a 1-indexed timeline), and interpolation is set per-keyframe according to `inp_fn`'s low byte (Linear vs. Bezier/Spline).

---

## 7. Stage Files (n.rel / d.rel)

Stage geometry files share the name convention `n.rel` (and, for DC only, a paired `d.rel`), but the internal byte layout differs completely across the three platforms.

### 7.1 BB (PC) n.rel

Single-file format, little-endian.

**Footer**: the last 16 bytes of the file; the first `u32` in the footer is the absolute offset of a **format/section table**.

**Format/section table** (at the footer-indicated offset):
```
u32 fmt2                (format marker/version, not used directly)
u32 n_count              (unused by this reader — see BB has one file, not paired n/d)
u32 d_count              (number of "D-section" descriptors)
u32 hd                   (unused)
u32 d_section_offset
u32 n_section_offset     (unused by this reader)
```

**D-section descriptor** (read `d_count` of these at `d_section_offset`), each:
```
i32 section_id
f32 pos_x, pos_y, pos_z
i32 rot_x, rot_y, rot_z    (BAMS)
f32 radius
u32 static_mesh_list_offset
u32 animated_mesh_list_offset
u32 static_mesh_count
u32 animated_mesh_count
u32 end_marker
```
Build a transform matrix from `pos`/`rot` (rotate then translate — see §1.2) for this section. Then, at `static_mesh_list_offset`, read `static_mesh_count` entries, each: `u32 node_offset` followed by 12 bytes ignored (per-entry attribute data not used for static import). For each `node_offset`, seek there and recursively read the node tree (§7.1.1) using the section's matrix as the initial parent transform.

(Animated-mesh lists exist in the file but are not traversed by this reader for BB stages — only the static list is imported.)

#### 7.1.1 BB stage node tree

Same 52-byte node layout as NJ's Euler node (§2.3): flags, mesh_offset, pos, BAMS rot, scale, child_offset, sibling_offset. Same flag semantics (`0x01` skip-translate, `0x02` skip-rotate). Same depth-first traversal with the same bounds-check-and-abandon-subtree rule as NJ (§2.3 step 2), except BB's bounds check uses **strict `<`** against a hardcoded minimum node size of 52 bytes remaining, rather than validating each of the three offset fields individually against the payload size before recursing — practically equivalent in effect.

#### 7.1.2 BB stage mesh sub-chunk

```
u32 flags
u32 vertex_info_list_offset
u32 vertex_info_count
u32 triangle_strip_list_a_offset
u32 triangle_strip_a_count
u32 triangle_strip_list_b_offset
u32 triangle_strip_b_count
f32 center_x, center_y, center_z
f32 radius
```
(36 bytes — identical shape to XJ's mesh chunk, §3.2.)

#### 7.1.3 BB vertex info list — the multi-entry quirk

If `vertex_info_list_offset != 0`, read **all** `vertex_info_count` entries (not just the first), each entry 16 bytes at `vertex_info_list_offset + entry_index * 16`:
```
u16 vtype
u16 unknown
u32 vertex_data_offset
u32 vertex_size
u32 vertex_count
```
`vtype` bits: `0x01` = has UV, `0x02` = has normal, `0x04` = has color.

**Critical quirk**: a stage mesh may legitimately have `vertex_info_count > 1`. Observed in real files: entry 0 might carry `vtype` with the UV bit set (used by textured strips) while entry 1 (same vertex count and same index range) carries a `vtype` *without* the UV bit (used by untextured/shadow strips referencing the same vertex positions). Both entries describe overlapping vertex-index ranges into the same per-mesh vertex stack.

The correct behavior: **merge** entries into one running per-index vertex stack, keyed by vertex index (0-based per entry), such that:
- Position, normal, and color are always overwritten by whichever entry is processed for that index (last-write-wins per field, since a later entry typically shares the same values or the field is simply absent).
- **UV is preserved if any earlier-processed entry already set it** — a later entry lacking UV data (its `vtype` has the UV bit clear) must **not** null out UV data written by an earlier entry for the same vertex index. Concretely: when starting a vertex record, check whether an entry already exists at that index in the stack; if so, carry its `uv` field forward as the new record's starting `uv` value (only overwritten if the *current* entry itself also has the UV bit set).

Getting this wrong (only reading the first vertex-info entry, or letting a later no-UV entry clobber an earlier UV) silently drops UV coordinates from meshes — with no error, just a texture-less/incorrectly-mapped mesh. When a mesh's final combined vertex list has *some* vertices with real UV data and others without (from a mixed-entry situation, or a single vertex index that legitimately never received UV from any entry), fill the gaps with `(0.0, 0.0)` rather than dropping the UV attribute for the whole mesh — dropping it entirely would be a worse regression than a handful of vertices with a placeholder UV.

Each vertex record, in read order once at `vertex_data_offset + index * vertex_record_size`:
1. `f32 x, y, z` — transformed to world space by the section/node's world matrix.
2. If `vtype & 0x02`: `f32 nx, ny, nz` — transformed by the world-matrix rotation only.
3. If `vtype & 0x04`: 4 bytes in **BGRA** order, normalized.
4. If `vtype & 0x01`: `f32 u, f32 v`; V flipped (`1.0 - v`).

#### 7.1.4 BB strip lists & materials

Strip-list entries are 20 bytes each (identical shape to XJ's strip-info, §3.4):
```
u32 material_property_list_offset
u32 material_property_list_size
u32 index_list_offset
u32 index_count
u32 unknown
```
Index list: `index_count` signed 16-bit vertex-stack indices. Strip → triangle conversion: **alternating winding** per consecutive triple (same style as NJ VOLUME/STRIP chunks, §2.5), not the XJ double-winding approach — for `i` in `0..count-3`, alternate between `(i+1, i+2, i)` and `(i, i+2, i+1)` as the flip toggles each step; degenerate triangles (any two of the three indices equal) are dropped.

Material property blocks (read `material_property_list_size` of these at `material_property_list_offset`), each starting with a `u32 mat_type` tag:

| `mat_type` | Payload | Meaning |
|---|---|---|
| 2 | `u32 dst, u32 src`, then 4 bytes ignored | Blend mode: `src==1,dst==4` → additive; `src==5,dst==4` → normal |
| 3 | `u32 tex_id`, then 8 bytes ignored | Texture index (no masking) |
| 4 | 12 bytes ignored | Marks material as double-sided |
| 5 | 4 bytes RGBA (normalized), then 8 bytes ignored | Diffuse color |
| other | 12 bytes ignored | |

### 7.2 DC (Dreamcast) n.rel / d.rel

DC stages are split across **two sibling files**: `xxxn.rel` (normal/static content) and `xxxd.rel` (a second, typically animated/dynamic content, geometry pool) — detected by matching filename stems that differ only in a trailing `n` vs `d` (case-insensitive, since real-world filenames like `MAP_LOBBY00N.REL` exist in uppercase). Both files, when present, are parsed independently but merged into one shared "sections" map keyed by section ID.

Each file (`n` or `d`) uses the **same footer/table/section-descriptor scheme** as BB (§7.1), with one difference: the section descriptor's static-mesh-list and animated-mesh-list entries have different trailer sizes (`0x2C` bytes skipped per static entry instead of BB's implicit 12; `0x34` bytes skipped per animated entry, plus an extra leading `u32` that BB's static-only reader doesn't have) — reflecting that DC's per-file section table carries both lists uniformly, whereas the addon's BB reader only consumes the static list.

For each section ID encountered (from either the n or d file), accumulate its static+animated mesh entries into one combined list, tagging each entry with which file it came from. Once both files are fully scanned, iterate every accumulated section and, for each of its mesh entries, seek into whichever file (`n` or `d`) that entry came from and read the node tree — reusing the **exact same node/mesh/vertex-list/strip reading code as NJ's NJD chunk grammar** (§2.5), because DC stage geometry is stored in the NJD chunk format, not the BB/XJ-style explicit vertex-info-list format. In other words: DC `n.rel`/`d.rel` files are structurally "an NJ NJCM bone tree without the outer NJTL/NJCM chunk wrapper" — the node/mesh reading entry point is called directly at each mesh-list offset instead of being discovered via chunk magic scanning.

Texture names for DC stages are read from an embedded name list similar to NJTL (offset+count table of string pointers) located via the same footer/table mechanism, at a `texture_ofs` field in the table.

### 7.3 GC (GameCube) n.rel — standard Flipper format

Big-endian throughout, single file (no `d.rel` sibling for GC).

**Footer**: last 16 bytes; first `u32` (BE) is the format-table offset.

**Format table** (BE):
```
u32 fmt2
u32 n_count               (unused)
u16 d_count
u16 padding
u32 hd                    (unused)
u32 d_section_offset
u32 texture_table_offset
```

**Texture name table** (at `texture_table_offset`):
```
u32 name_list_offset
u32 name_count
```
Seek to `name_list_offset`; read `name_count` entries, each: `u32 name_offset`, then 8 bytes skipped (mirrors the NJTL-style layout, §2.2, but big-endian). For each name, seek and read a null-terminated string; assign to the pre-loaded texture archive by array position (index-for-index), stripping the extension from the name for display.

**Section table** (at `d_section_offset`, `d_count` entries), each:
```
i32 section_id
f32 pos_x, pos_y, pos_z
i32 rot_x, rot_y, rot_z    (BAMS)
f32 radius
u32 list_a_offset
u32 list_b_offset
u32 list_a_count
u32 list_b_count
u32 end_marker
```
Build the section's transform matrix (rotate then translate) as the initial parent for all meshes in this section.

**List A** (static meshes) — `list_a_count` entries at `list_a_offset`, each 16 bytes: `u32 node_offset, u32 attr1, u32 attr2, u32 flags`. `attr1`/`attr2` are unused by the reader.

**List B** (animated meshes) — `list_b_count` entries at `list_b_offset`, each 32 bytes: `u32 node_offset, u32 anim_offset, u8[8] unused, f32 speed, u8[8] unused, u32 flags`.

**Flag-based skip filtering**: certain `flags` values are known to be non-renderable/placeholder entries and are unconditionally skipped:
- List A skip set: `0x010225`, `0x010204`, `0x010205`, `0x010264`
- List B skip set: `0x010244`, `0x010204`
- Additionally, for *both* lists, any entry whose `flags & 0x200` is set is skipped, regardless of the exact value (a general "hidden/disabled" bit).

For each non-skipped entry in both lists, seek to `node_offset` and read the node tree using the **same GJCM node/mesh/strip readers described in §4.3–§4.8** (parent = the section's transform matrix). GC stage files thus reuse the actor-model GJ node format entirely — the only file-specific part is this outer footer/section/list container.

### 7.4 GC Episode III — Card Revolution n.rel variant

Episode III (Card Revolution) uses **completely different, much smaller** `n.rel` files that are **not** parsed by the standard GJCM stage reader in §7.3 — they contain no `GJCM`/`GJTL` chunk magic anywhere in the file. Observed characteristics of real Episode III files:

- File size can be as small as ~600 bytes (a lightweight scene descriptor, not embedded 3D geometry — actual mesh data for these stages lives in separately-named `.gj` files in the same directory) up to several hundred kilobytes for larger maps.
- The file begins with an ASCII string such as `"map_sky\0"` (a scene-name tag), not any of the standard geometry magics.
- Contains big-endian floating point values (bounding-box-like ±50.0 range values have been observed) somewhere in the body.
- Contains the ASCII string `"fmt2"` — this is the **same `fmt2` marker field** that appears at the start of the standard GC stage footer-table (§7.3), confirming these files still use the shared GC footer/table convention even though their body layout differs from the standard section/mesh-list scheme.

**Detection heuristic** (used because there is no unique magic byte sequence to scan for): read the file's last 16 bytes as big-endian; the first `u32` there is a byte offset. If that offset, when read as 4 bytes at that position, equals the ASCII string `"fmt2"`, the file uses the shared GC footer convention and should be treated as a GC-platform file for the purposes of texture-archive/importer selection — even though it will not successfully parse via the standard `parse_stage` node/section reader in §7.3, since its body past the footer does not follow that section-list layout. A full parser for the Episode III body format has not yet been reverse-engineered beyond this footer-detection level; implementers wanting complete Episode III scene support will need to reverse-engineer the region between the header string and the shared footer independently.

---

## 8. Texture Archives

Three archive container formats exist, auto-detected by magic bytes at the start of the file: `XVMH`/`XVRT` → XVM; `GVMH`/`GVRT` → GVM; anything else is assumed to be PVM (PVM has no reliable magic at offset 0 in all variants, so it is the fallback).

All three loaders return a list of `{name, width, height, pixels}` dicts, where `pixels` is a flat RGBA byte buffer, **top-to-bottom row order** (row 0 = topmost).

### 8.1 XVM (PC/Blue Burst)

Little-endian.

**Header**: `u32 magic "XVMH"`, `u32 archive_length` (unused), `u32 texture_count` (unused — the loader scans for chunks directly instead of trusting this count).

Scan the remainder of the file for `XVRT` sub-chunks: `u32 magic`, `u32 chunk_length`, record the position right after the length field as this texture's data start.

**Per-texture header** (at each recorded position):
```
u32 format_1        (color format — not used for decoding; DXT variants store color mode elsewhere)
u32 format_2        (compression type: 6 = DXT1, 7 = DXT3, 8 = DXT5; anything else defaults to DXT1)
u32 tex_id           (index within archive — unused, textures are named sequentially instead)
u16 width
u16 height
u32 payload_size
u8  padding[0x24]
u8  payload[payload_size]
```
Decode `payload` with the DXT variant selected by `format_2` (§8.4). Textures are named `"Texture_N"` where N is their position in decode order (no name table exists in XVM).

### 8.2 PVM (Dreamcast, PowerVR)

Little-endian. Two sub-variants:

**Bare single-texture file** (starts with `PVRT` directly, no `PVMH` wrapper): `u32 magic "PVRT"`, `u32 chunk_length`, then a per-texture header: `u8 color_format, u8 data_format, [1 unused byte pair — header continues at +4]`, `u16 width, u16 height` at offset +4 from chunk start, payload immediately after. Decoded with `decode_pvrt` (§8.5); named `"texture_000"`.

**Multi-texture archive** (has a `PVMH` chunk): scan byte-by-byte for the `PVMH` tag (it is not guaranteed to be at offset 0 — there can be leading bytes). Once found:
```
u32 pvmh_chunk_length
u16 flags
u16 texture_count
```
Then `texture_count` entries, each starting with `u16 index`, followed by optional fields gated by `flags` bits (checked in this order):
- `flags & 0x08`: `char name[0x1C]` (28 bytes, null-padded ASCII)
- `flags & 0x04`: `u16` (pixel-format hint — read and discarded)
- `flags & 0x02`: `u16 size_code` — width = `1 << ((size_code & 0xF) + 2)`, height = `1 << (((size_code >> 4) & 0xF) + 2)`
- `flags & 0x01`: `u32` — replaces the entry's `index` value (a GUID/alternate-index field)

After the entry table, scan for `PVRT` sub-chunks (one per entry, in order). **Quirk**: the position to resume scanning from should be `max(current_read_position, pvmh_start + pvmh_chunk_length)` — some real DC PVM files count `pvmh_chunk_length` from the start of the `PVMH` tag itself (including the 8-byte magic+length prefix), while the entry-table parse position is measured from just after that prefix; naively trusting `pvmh_start + pvmh_chunk_length` alone can overshoot the first `PVRT` chunk by 8 bytes on such files, so take whichever of the two candidate positions is larger.

For each `PVRT` chunk found: `u32 magic "PVRT"`, `u32 payload_length`, then the same per-texture header/payload as the bare-file case (`u8 color_format, u8 data_format`, `[pad]`, `u16 width, u16 height` at +4, payload at +8). Decode with `decode_pvrt` (§8.5). Name = the corresponding entry's name (extension stripped if present), or `"texture_NNN"` if no name was available.

### 8.3 GVM (GameCube)

Same overall shape as PVM but **big-endian** for header/entry-table fields (though individual GVR texel data formats specify their own endianness per format — most are big-endian 16-bit values, see §8.6).

**Bare single-texture file** (starts with `GVRT`): `u32 magic`, `u32 chunk_length` (LE, unusually — the length field for this bare-file variant is read little-endian even though everything else in GVM is big-endian), then per-texture header at chunk-start+4: `pixel_format = byte[2] >> 4`, `data_format = byte[3]`, `u16 width, u16 height` (BE) at +4, payload at +8. Named `"texture_000"`.

**Multi-texture archive** (has `GVMH`): scan for the tag (may not be at offset 0). Then:
```
u32 chunk_length     (LE — same quirk as bare-file variant)
u16 flags            (BE)
u16 texture_count    (BE)
```
Entry table, `texture_count` entries, each `u16 index` (BE) followed by optional fields (same flag bits and field shapes as PVM's entry table, §8.2, but multi-byte fields are BE): name (`flags & 0x08`), pixel-format hint (`flags & 0x04`), size code (`flags & 0x02`, same width/height decode formula as PVM), GUID/index override (`flags & 0x01`, 4 bytes).

Resume-scan position uses the same `max(current_pos, gvmh_start + chunk_length)` rule as PVM.

For each `GVRT` sub-chunk: `u32 magic`, `u32 payload_length` (LE), then per-texture header at chunk-start: `pixel_format = byte[2] >> 4`, `data_format = byte[3]`, `u16 width, u16 height` (BE) at +4, payload at +8, length = `payload_length - 8`. Decode with `decode_gvr` (§8.6).

### 8.4 DXT1/DXT3/DXT5 decoding (XVM)

All three formats decode in 4×4-pixel blocks, laid out left-to-right, top-to-bottom, `ceil(width/4) × ceil(height/4)` blocks total.

**DXT1** (8 bytes/block): two RGB565 color endpoints `c0`, `c1` (each a `u16`), then a `u32` of 2-bit-per-pixel indices (16 pixels, LSB pixel first in bit order). Build a 4-color palette:
- If `c0 > c1` (as raw u16 values): `[c0, c1, (2·c0+c1)/3, (c0+2·c1)/3]`, all opaque.
- Else: `[c0, c1, (c0+c1)/2, transparent-black]` — the 4th palette entry is used for punch-through alpha.

**DXT3** (16 bytes/block): 8 bytes of explicit 4-bit-per-pixel alpha (16 pixels, 2 pixels/byte, low nibble first within each row-pair... concretely: for each of 4 rows, one `u16` holds 4 pixels × 4 bits, alpha = nibble × 17 to expand 0–15 to 0–255), followed by a standard DXT1-style 8-byte color block **always treated as 4-color mode** (the `c0 > c1` transparent-4th-color special case is disabled — alpha comes entirely from the explicit block).

**DXT5** (16 bytes/block): 2 alpha reference bytes `a0`, `a1`, then 6 bytes (48 bits) of 3-bit-per-pixel alpha indices for the 16 pixels. Build an 8-entry alpha palette:
- If `a0 > a1`: `[a0, a1, interpolate 6 steps between them (7ths)]`
- Else: `[a0, a1, interpolate 4 steps (5ths), then hard 0, then hard 255]`
followed by a DXT1-style color block, again always in 4-color mode (alpha supplied separately).

### 8.5 PVR (PowerVR, Dreamcast) decoding

`decode_pvrt(payload, color_format, data_format, width, height)`:

**Color formats** (`color_format`, applied to each raw 16-bit texel value after the pixel layout below decodes its position):
- `0`: ARGB1555 — 1-bit alpha (0 or 255), 5-5-5 RGB.
- `1`: RGB565 — no alpha (opaque).
- `2`: ARGB4444 — 4-4-4-4; if RGB is non-zero but alpha would be 0, alpha is forced to 255 (avoids fully-transparent-but-visible-color artifacts from source data that never intended alpha=0).

**Data/pixel-layout formats** (`data_format`), each may additionally have mipmaps prepended (smallest level first) that must be skipped past to reach full-size data — the mip-skip byte count is computed by summing, from the smallest level up to (but not including) the full-size level, `width_at_level * height_at_level / 4` bytes (if VQ/compressed) or `* 2` bytes (if raw), plus one final byte or `u16` for the 1×1 level; the full-size level itself is not skipped:
- **Twiddled** (untiled Morton-order swizzle) formats (`0x01, 0x02, 0x0D, 0x12`): non-VQ, mip flag set for some values (`0x02, 0x12`) — for each output `(x, y)`, compute a Morton-interleaved index (interleave bits of `x` and `y`: `untwiddle(x,y) = interleave_bits(y) | (interleave_bits(x) << 1)`), read one 16-bit texel from `payload[index*2 .. ]`, decode via `color_format`.
- **Rectangle** (`0x09`): plain row-major 16-bit texels, no twiddling, no mipmaps.
- **VQ** (Vector Quantization, `0x03, 0x04, 0x10, 0x11`) — codebook-based compression:
  1. The **codebook is always located at the very start of the payload** (offset 0), **before** any mipmap data — this is true even for VQ+Mip formats, where naively skipping mip data first (as done for non-VQ mip formats) before reading the codebook silently reads garbage as codebook entries and produces badly corrupted textures across nearly every affected texture in a stage. Always read the codebook first, then skip mips, never the reverse.
  2. Codebook size: 256 entries normally; for "SmallVQ" formats (`0x10`, `0x11`), the codebook size instead depends on the texture's width: `<=16` → 16 entries, `==32` → 32 entries, `==64` → 128 entries (implicitly 256 for anything else/larger).
  3. Each codebook entry is a 2×2 block of 4 texels (a "quad"), each texel a 16-bit value decoded via `color_format` — read 4 × `u16` per entry, in order top-left, top-right, bottom-left, bottom-right (matching the quad-write order below).
  4. **After** reading the codebook, if this format also has mipmaps (`data_format` is in the mip-flagged set), skip the *compressed* (VQ-style, i.e. 1 byte per 2×2 quad) mip data for all levels below full size.
  5. The remaining data is one **byte index per 2×2 output quad**, in twiddled (Morton) order over a `(width/2) × (height/2)` grid of quads: for each twiddled quad position, read one byte, modulo it by the codebook size (defensive clamping), and write that codebook entry's 4 texels into the corresponding 2×2 region of the output image.

### 8.6 GVR (GameCube) decoding

`decode_gvr(payload, pixel_format, data_format, width, height)` — GC textures are always stored in **tiled** blocks (never linear/twiddled like PVR), tile size depends on `data_format`:

| `data_format` | Name | Tile size | Bits/pixel | Notes |
|---|---|---|---|---|
| `0x00` | I4 | 8×8 | 4 | Grayscale; nibble × 17 expands 0–15 to 0–255; one byte holds 2 horizontally-adjacent pixels (high nibble first) |
| `0x01` | I8 | 8×4 | 8 | Grayscale, one byte/pixel |
| `0x02` | IA4 | 8×4 | 8 | Grayscale+alpha; one byte/pixel, high nibble = intensity, low nibble = alpha, each ×17 |
| `0x03` | IA8 | 4×4 | 16 | Grayscale+alpha; one BE `u16`/pixel, high byte = alpha, low byte = intensity |
| `0x04` | RGB565 | 4×4 | 16 | One BE `u16`/pixel, standard 5-6-5, opaque |
| `0x05` | RGB5A3 | 4×4 | 16 | One BE `u16`/pixel: if bit 15 set → RGB555 opaque; else → 3-bit alpha (×36.4≈/7) + 4-4-4 RGB |
| `0x06` | RGBA8 | 4×4 | 32 | Two 32-byte sub-blocks per tile: first sub-block holds interleaved (A,R) byte pairs for all 16 pixels, second holds (G,B) pairs; combine by pixel index |
| `0x0E` | CMPR | 8×8 (as 2×2 grid of 4×4 DXT1 sub-blocks) | ~4 | GC's DXT1 variant — see below |
| other | — | — | — | Unsupported: fill the whole texture with opaque magenta as a visual "missing format" marker |

**Tiled traversal** for formats other than CMPR: iterate output in tile-sized blocks (`by` stepping by tile height, `bx` stepping by tile width, both covering the full image), and within each tile iterate its rows/columns in raster order, consuming input bytes sequentially (i.e. input is stored one full tile at a time, tiles in row-major block order, pixels within a tile in row-major order).

**CMPR (0x0E)** — GC's DXT1 variant, tiled in 8×8 "super-tiles", each super-tile holding a 2×2 grid of independent 4×4 DXT1 sub-blocks (top-left, top-right, bottom-left, bottom-right, in that read order): each sub-block is 8 bytes — two big-endian RGB565 color endpoints, then a 4-byte index table where **each row byte's 2-bit indices are packed MSB-first** (pixel 0 of the row occupies bits 7:6, not bits 1:0 as in standard little-endian DXT1). Palette construction follows the same `c0 > c1` rule as PC DXT1 (§8.4).

### 8.7 XVR bare-file / GVR bare-file fallbacks

Both PVM and GVM loaders fall back to treating the file as a single bare texture (no archive wrapper) if the very first 4 bytes are the individual-texture magic (`PVRT` for PVM, `GVRT` for GVM) rather than the archive-header magic (`PVMH`/`GVMH`) — see §8.2/§8.3.

---

## 9. Platform & File Autodetection

Given only a filepath (and, for `.rel` files, permission to read the file and list its directory), determine which of BB/DC/GC platform rules to apply:

1. **By extension**, for actor/model files, extension alone is authoritative:
   - `.xj` → BB
   - `.nj` → DC
   - `.gj` → GC
2. **For `.rel` stage files** (ambiguous by extension alone — all three platforms can produce a file ending in `.rel`):
   a. **DC sibling check**: if the filename stem's last character (case-insensitive) is `n` or `d`, compute the "other" stem by swapping that trailing letter (`n`↔`d`) and check (case-insensitively) whether a file with that other stem and a `.rel` extension exists in the same directory. If so, this is a DC paired n/d stage → **DC**.
   b. **GC magic-byte scan**: read the whole file and check whether the literal bytes `GJCM` or `GJTL` appear anywhere in it. If so → **GC** (standard Flipper stage format, §7.3).
   c. **GC Episode III footer check**: if the file is at least 20 bytes, read the last 16 bytes; take the first big-endian `u32` there as a candidate offset, and check whether the 4 bytes at that offset equal the ASCII string `fmt2`. If so → **GC** (Episode III variant, §7.4), even though no `GJCM`/`GJTL` magic is present.
   d. **Default**: if none of the above match → **BB**.

This ordering matters: DC-sibling-file detection must be attempted before the GC magic scan, since some legitimately DC-paired files could coincidentally satisfy neither GC check but must not fall through to the BB-only stage reader (which expects a completely different table format, §7.1, and would produce garbage offsets — historically manifesting as `struct.unpack_from` failures requesting absurd buffer sizes like offset `0xF0000000`, from misinterpreting a big-endian GC footer value as little-endian).

All directory-listing comparisons in this section must be done **case-insensitively** — real game files exist with fully-uppercase names (e.g. `MAP_FOREST01N.REL`) and case-sensitive matching silently fails to find the DC sibling or breaks texture-archive discovery on such filesystems/archives.

---

## 10. Texture-to-Model Association Strategies

Across formats, textures reach a model through several different mechanisms, and a general-purpose importer needs to try all of them in a sensible priority order because any given file may use only one:

1. **Embedded-in-BML pairing** (§5.4/§5.5): the texture archive is a sibling entry inside the same BML container, either immediately following the model entry (standard case) or matched by a compound-name convention elsewhere in the archive.
2. **Per-model sidecar file** (§1.3, §5.5 step 2): a texture archive file on disk with the same base name as the model.
3. **BML-level shared sidecar** (§5.5 step 3): one texture archive shared by every model inside a BML, with the same base name as the `.bml` file itself, used as a fallback whenever a specific model has no more specific texture source. This is the common case for GameCube prop packs where dozens of small models all draw from one large shared GVM.
4. **LOD/variant reuse** (§5.5 step 4): a model with no texture source of its own inherits the immediately-preceding model's resolved textures — used for `lo_`/`hi_`/`sd_`-prefixed LOD variants that appear right after their base model in archive order.

Which texture **index** a given strip/material actually refers to (§4.7, type `0x08` material property) depends on whether the model's own GJTL name list was present:
- **GJTL present**: the model's texture indices are local to its own GJTL list. If the texture archive that was pre-loaded for this model has real names (not generic placeholders) and those names don't already match array position 1:1 with GJTL order, the addon remaps the loaded-texture array by matching each GJTL name (case-insensitive) against the archive's texture names, so that index 0 in strip data → whichever archive texture has the same name as GJTL name 0, and so on (§4.2). If the archive's names are all generic placeholders, no remapping occurs (indices are trusted as-is, in raw archive order).
- **GJTL absent** (common for individual props extracted from a BML with a shared sidecar archive): there is no local name list to be relative to, so the raw index value in strip data must be interpreted as an **absolute index directly into the shared texture archive**. An importer that always validates texture indices against an (in this case, empty) local GJTL name-count, rather than falling back to the loaded archive's actual size when no GJTL exists, will incorrectly reject every texture index as out-of-range and produce fully texture-less models — this is a real, previously-encountered bug class, not a hypothetical one, and is the reason the size-of-texNames-else-size-of-textures fallback (§4.7) exists.
