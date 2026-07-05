# Phantasy Star Online Model, Stage, Texture & Animation Format Specification

This document is a standalone, byte-accurate specification for the model, stage,
texture, and animation formats used by Phantasy Star Online (PSO) across three of
its platform releases: **Dreamcast (DC)**, **PC / Blue Burst (BB)**, and
**GameCube / Episode I & II & III (GC)**. It is intended to allow an independent
programmer to write a parser or importer for any of these formats without
access to the PSO Ultimate Importer's Python code.

All multi-byte numeric fields are little-endian unless stated otherwise.
GameCube-format chunks (GJ*, GVM, and NJ payloads embedded in GC files) are
**big-endian** (PowerPC). Floats are IEEE-754 32-bit. Strings are
null-terminated ASCII unless stated otherwise.

## Table of Contents

1. [Shared Utilities & Conventions](#1-shared-utilities--conventions)
2. [NJ — Dreamcast Ninja Model Format](#2-nj--dreamcast-ninja-model-format)
3. [XJ — Blue Burst / PC Ninja Model Format](#3-xj--blue-burst--pc-ninja-model-format)
4. [GJ — GameCube Flipper Model Format](#4-gj--gamecube-flipper-model-format)
5. [BML — Multi-Model Archive](#5-bml--multi-model-archive)
6. [NJM — Ninja Motion / Animation Format](#6-njm--ninja-motion--animation-format)
7. [.rel Stage Files (All Platforms)](#7-rel-stage-files-all-platforms)
8. [Texture Archive Formats (XVM / PVM / GVM)](#8-texture-archive-formats-xvm--pvm--gvm)
9. [Platform / File Autodetection Logic](#9-platform--file-autodetection-logic)
10. [Texture-Archive-to-Model Association Strategies](#10-texture-archive-to-model-association-strategies)
11. [GSL Archive Format](#11-gsl-archive-format)

---

## 1. Shared Utilities & Conventions

### 1.1 Magic constants

| Constant | Hex (as read, little-endian u32) | ASCII | Used by |
|---|---|---|---|
| `MAGIC_XVMH` | `0x484D5658` | `"XVMH"` | PC texture archive header |
| `MAGIC_XVRT` | `0x54525658` | `"XVRT"` | PC texture archive per-texture chunk |
| `MAGIC_NJCM` | `0x4D434A4E` | `"NJCM"` | NJ/XJ geometry chunk |
| `MAGIC_NJTL` | `0x4C544A4E` | `"NJTL"` | NJ/XJ texture-name list chunk |
| `MAGIC_NMDM` | `0x4D444D4E` | `"NMDM"` | NJ/XJ motion/animation chunk |
| `MAGIC_GVMH` | `0x484D5647` | `"GVMH"` | GC texture archive header |
| `MAGIC_GVRT` | `0x54525647` | `"GVRT"` | GC texture archive per-texture chunk |
| `MAGIC_GJTL` | `0x4C544A47` | `"GJTL"` | GJ texture-name list chunk |
| `MAGIC_GJCM` | `0x4D434A47` | `"GJCM"` | GJ geometry chunk |
| `MAGIC_POF0` | `0x30464F50` | `"POF0"` | Pointer-relocation chunk (some NJ/GJ files) |

All of these magics appear as literal 4-byte ASCII tags in the file (e.g. bytes
`4E 4A 43 4D` = "NJCM"); the "hex" column above is the little-endian
`uint32` value you get by reading those 4 bytes as an LE integer, which is how
the reference importer compares them.

### 1.2 BitStream reader semantics

All formats are read through a single seekable byte-stream abstraction with:

- `seek(offset, whence)` — `whence=0` absolute, `whence=1` relative to current position.
- `readString()` — reads bytes until a `0x00` terminator (exclusive), returns ASCII text.
- `readUInt`/`readInt` (4 bytes), `readUShort`/`readShort` (2 bytes), `readFloat` (4 bytes, IEEE-754), `readUByte` (1 byte).
- Endianness is chosen once per stream instance (`'<'` or `'>'`) and applies to every multi-byte read from that stream.

Nearly every parser in this spec is built by instantiating one of these streams
over a byte range and reading sequentially, occasionally seeking to
offsets stored earlier in the same stream (a classic "pointer soup" binary
format).

### 1.3 Angle encoding (BAMS)

Nearly all rotation fields across NJ/GJ/rel formats are stored as **signed
32-bit integers ("BAMS" — Binary Angle Measurement System)** representing a
fraction of a full turn in units of `1/65536` turn:

```
radians = raw_int32 * (2*PI / 65536)
```

This conversion constant recurs throughout the codebase as
`c = 2.0 * math.pi / 0x10000`.

### 1.4 DashMat4 — 4x4 transform matrix and composition order

The importer's matrix class (`DashMat4`) represents a 4x4 row-vector matrix
(`v' = v * M`), stored as `mtx[row][col]`, row-major, with translation in
`mtx[3][0..2]` (i.e. a standard row-vector affine matrix as used by
DirectX-style pipelines).

Operations:

- `scale(v)` — pre-multiplies a scale matrix.
- `translate(v)` — pre-multiplies a translation matrix (translation goes in row 3).
- `rotate(v)` — builds three axis matrices and multiplies them together in this exact order: **Rx, then Ry, then Rz** (`self.mtx = self.mtx * Rx; *= Ry; *= Rz`), i.e. `M = Rx * Ry * Rz` applied via successive `multiply()` calls. This is intrinsic ZYX / extrinsic XYZ euler order.
- `rotate4(vec3, w)` — builds a rotation matrix directly from a quaternion `(x,y,z,w)` using the standard quaternion-to-matrix formula (used only by DC `.nj` bones flagged as quaternion-rotation nodes).
- `compose(parent)` — **post-multiplies** the current matrix by the parent's matrix: `self.mtx = self.mtx * parent.mtx`. This is how a per-node local transform is converted to a world-space transform while walking the bone/node hierarchy parent→child.
- `transformPoint(p)` / `transformNormal(n)` — apply the composed matrix to a 3D point (including translation) or a 3D direction vector (rotation/scale only, ignoring translation), using row-vector convention (`p' = p*M[0..2][*] + M[3][*]`).

**Critical convention: for every node/bone type in this codebase, the
local transform is built as `rotate()` (or `rotate4()` for quaternion nodes)
FIRST, then `translate()` SECOND**, i.e. `M_local = R * T`. The composed world
matrix is then `M_world = M_local * M_parent`. This ordering must be
reproduced exactly or bone chains and vertex transforms will be wrong.

Two special flag bits control whether rotation/translation are applied at all
for a given node (see NJ/GJ node descriptions below):
- flag bit `0x02` (`NJD_...` "no rotate") — if set, skip the `rotate()` call for this node (treat local rotation as identity).
- flag bit `0x01` (`NJD_...` "no translate") — if set, skip the `translate()` call for this node.

### 1.5 PRS / LZS decompression (`decompress_prs`)

BML archives (and possibly other SEGA-era formats) use SEGA's "PRS" LZ77-style
compression, a bit-oriented variant of LZSS. Algorithm:

```
read a control bit (LSB-first from a byte, refilling the 8-bit
"cmd_byte" register whenever it's exhausted):

loop until input exhausted:
  bit = next control bit
  if bit == 1:
      literal byte: copy next input byte directly to output
  else:
      bit2 = next control bit
      if bit2 == 1:
          # "long" back-reference
          a = next input byte
          b = next input byte
          offset = (b << 8 | a) >> 3      # 13-bit distance, shifted
          amount = a & 7
          if amount == 0:
              amount = (next input byte) + 1
          else:
              amount += 2
          copy_start = output_length - 0x2000 + offset
      else:
          # "short" back-reference (2-bit length field)
          amount = (next control bit) << 1 | (next control bit)
          offset = next input byte
          amount += 2
          copy_start = output_length - 0x100 + offset
      copy `amount` bytes from output[copy_start:] to the end of
      output, one byte at a time (so overlapping copies replicate
      correctly, as in standard LZ77)
```

Notes:
- The "short" reference window is 256 bytes back (`0x100`); the "long"
  reference window is 8192 bytes back (`0x2000`).
- Any copy-source index that lands before the start of the output buffer
  reads as `0` (implementation detail — should not normally occur with
  well-formed data).
- There is no explicit end-of-stream marker beyond exhausting the input byte
  buffer.

### 1.6 `find_tex_archive` — texture archive auto-discovery by name

Given a model or stage filepath and a target platform (`'BB'`, `'DC'`, or
`'GC'`), locates a texture archive file in the same directory:

1. **Build stem candidates**, most specific first:
   - If the file stem's last character (case-insensitively) is one of `n`,
     `d`, `c`, `r` (a Ninja "type letter" suffix), strip it to get `base`.
     Add `base` as a candidate.
   - For BB-style stage naming (`forest_01` → `forest`), also try stripping a
     trailing `_` plus 1–3 digits from `base` (checked for `strip` lengths
     1..3, testing whether `base[-strip]` is literally `_`).
   - Always add the full original stem (no suffix stripped) as the final,
     lowest-priority candidate.
2. **Extension priority order** depends on platform:
   - `GC` → try `.gvm`, then `.xvm`, then `.pvm`
   - `DC` → try `.pvm`, then `.xvm`, then `.gvm`
   - `BB` (default) → try `.xvm`, then `.pvm`, then `.gvm`
3. For each extension (in priority order), try every stem candidate (in
   priority order) and return the first `stem+ext` combination that exists on
   disk.
4. **Fallback**: if no name-based match is found, scan the directory for any
   file whose extension matches one of the platform's priority extensions; if
   exactly one file of a given (highest-priority) extension exists in the
   directory, return it (a "there's only one texture archive here, it must be
   the right one" heuristic). Directories with zero or multiple ambiguous
   candidates return `None`.

### 1.7 `find_compound_tex_path` — compound-extension sidecar

Some texture archives are named with a *compound* extension directly appended
to the model's full filename (not replacing it), e.g. `model.nj.xvm` beside
`model.nj`. Checked (in this exact order, both cases): `.xvm`/`.XVM`,
`.gvm`/`.GVM`, `.pvm`/`.PVM`. Returns the first that exists, else `None`. This
check is always tried *before* `find_tex_archive`'s stem-based search.

### 1.8 `find_skybox_files`

Given an `n.rel` stage filepath, looks for a matching skybox model by
replacing the trailing `n.rel`/`N.REL` (5 characters, case preserved from the
original) with a base stem, then case-insensitively scanning the directory
for `base+"s"` with extension `.xj`, `.nj`, or `.gj` (tried in that order via
first-match-wins directory scan, not existence-check per extension — the
directory listing is scanned once and the first matching entry whose
extension is any of the three wins). A matching texture archive is looked up
the same way using extensions `.xvm`, `.pvm`, `.gvm`.

### 1.9 `detect_platform` — see [Section 9](#9-platform--file-autodetection-logic).

### 1.10 `bml_read` — see [Section 5](#5-bml--multi-model-archive).

---

## 2. NJ — Dreamcast Ninja Model Format

**Overview:** `.nj` is SEGA's "Ninja" chunk-based 3D model format used natively
on Dreamcast (PSO v1/v2). It is a hierarchical bone/node tree where each node
carries a transform and an optional pointer to a "mesh" (a chunk stream
describing vertices, materials, and polygon strips). The same chunk-reading
code (`NinjaChunkMixin`) is reused for `.nj` actor files, and for the mesh
payloads found inside DC `.rel` stage files (Section 7).

A `.nj` file is a flat sequence of top-level chunks, each with a 4-byte magic
tag and a 4-byte payload length, e.g.:

```
[NJTL magic][u32 length][... NJTL payload ...]
[NJCM magic][u32 length][... NJCM payload (bone tree) ...]
[NMDM magic][u32 length][... animation data (skipped by geometry importer) ...]
```

Unknown chunk magics are skipped by reading a u32 length immediately after the
magic and seeking past that many bytes.

### 2.1 Endianness auto-detection

DC `.nj` outer chunk headers (magic + length) are always little-endian, but
the *payload* of the `NJCM` chunk may internally be **big-endian** if the file
actually originates from a GameCube build (some GC `.nj`-extension files exist
this way; see the mixed-endianness comment in the importer). Detection
heuristic, applied to the first `NJCM` chunk found:

1. Read the first 48+ bytes of the payload as if it were the start of the
   root bone node, under both little-endian and big-endian interpretation.
2. For each interpretation, check:
   - `flags` (first u32) `<= 0x1FFF` (small, plausible flag bitmask),
   - `meshOfs` (second u32) `<= payload length`,
   - `scale.x` (float at byte offset 32 into the node, i.e. the first
     component of the node's `scl` field) is within `[0.1, 10.0]` in absolute
     value (real NJ scale values are almost always ~1.0).
3. Whichever interpretation satisfies all three first (little-endian is tried
   first) is used for the entire NJCM payload.

### 2.2 NJTL — texture name list chunk

Read from the start of the NJTL payload:

```
u32 list_ofs      # offset (relative to NJTL payload start) of the name-offset table
u32 texture_count
```

Then seek to `list_ofs`; for each of `texture_count` entries:
```
u32 name_string_offset   # relative to NJTL payload start
[8 bytes skipped — unused/reserved fields]
```
Finally, for each collected `name_string_offset`, seek there and read a
null-terminated ASCII string. These names are later matched (by stripping
directory and extension) against the loaded texture archive's textures, in
order, to assign human-readable names to `Texture_N` placeholders.

### 2.3 NJCM — bone/node hierarchy (`readBone`)

The NJCM payload is a tree of fixed-size node records, each optionally
pointing to a mesh chunk stream, a child node, and a sibling node — classic
first-child/next-sibling tree encoding. Two node layouts exist depending on a
rotation-type flag bit:

**Node flags** (first u32 field, `flags`):
| Bit | Meaning |
|---|---|
| `0x001` | Skip `translate()` for this node — pos field is present but should not be applied. |
| `0x002` | Skip `rotate()` for this node — rot field is present but should not be applied. |
| `0x400` | Rotation is stored as a **quaternion** (see below), not BAMS euler angles. This changes the node's byte layout (adds a trailing `w` float). |

**Standard (non-quaternion) node layout — 52 bytes total:**

```
offset  size  field         type/encoding
0       4     flags         u32 (see table above)
4       4     meshOfs       u32, offset (bytes, from NJCM payload start) to mesh data, or 0
8       4     pos.x         f32
12      4     pos.y         f32
16      4     pos.z         f32
20      4     rot.x         i32 BAMS  (* 2*PI/65536 → radians)
24      4     rot.y         i32 BAMS
28      4     rot.z         i32 BAMS
32      4     scl.x         f32
36      4     scl.y         f32
40      4     scl.z         f32
44      4     childOfs      u32, offset to first child node, or 0
48      4     siblingOfs    u32, offset to next sibling node, or 0
```

**Quaternion node layout (flags & 0x400) — 56 bytes total:** identical to the
above except the `rot` fields at offset 20–28 are read as 3 raw **floats**
(quaternion x, y, z components, NOT BAMS-scaled), and one extra trailing
4-byte float `w` (quaternion w) follows immediately after `siblingOfs` at
offset 52.

**Reading a node (`readBone`, recursive DFS):**

1. Bail if fewer than 52 bytes remain in the stream.
2. Read `flags`. If `flags & 0x400`, also require 56 bytes total (bail if not enough) and read the quaternion layout; else read the standard layout.
3. Build the node's local transform `mat`:
   - if `not (flags & 0x02)`: apply rotation — `mat.rotate4(rot_xyz, w)` for quaternion nodes, else `mat.rotate(rot_xyz_bams_radians)`.
   - if `not (flags & 0x01)`: `mat.translate(pos)`.
4. If a parent matrix was passed in, `mat.compose(parent_matrix)` (post-multiply by parent — see §1.4).
5. Record this node (its world position/axes derived from `mat`, and its *local* pos/rot for later animation retargeting) in DFS order — this ordering is the canonical "bone index" space used later by NJM animation import and by per-vertex bone-group assignment.
6. Bounds-check: if `meshOfs`, `childOfs`, or `siblingOfs` are `>=` the payload size, **abort recursion for this node and its subtree entirely** (do not attempt to read a mesh or recurse into children/siblings). This exact bail-out behavior (rather than only skipping the invalid pointer) matches the reference importer and is required to avoid spurious geometry on certain malformed/edge-case character models.
7. If `meshOfs != 0`, seek there and read the mesh chunk stream (`readChunks`, §2.4).
8. If `childOfs != 0`, seek there and recurse with `pMatrix = mat` (children inherit this node's world transform).
9. If `siblingOfs != 0`, seek there and recurse with `pMatrix` = the **same parent matrix this node received** (siblings share a parent, not each other).

### 2.4 Mesh: NJD chunk stream (`readChunks` / `NinjaChunkMixin`)

Once positioned at a mesh offset, a *variable-length stream of typed chunks*
follows until a terminator chunk (`NJD_CE = 255`) or the parser runs out of
buffer. Each chunk has a **chunk type** (`ch`, 0–255) and a **chunk flags**
byte (`cf`, 0–255), read together as one `(ch, cf)` pair per chunk, but the
exact byte layout of that pair — and whether a length word precedes or follows
it — depends on chunk category and on little- vs big-endian mode (see §2.4.1).

**Little-endian (native DC) chunk header:** simply two consecutive bytes:
```
u8 ch   # chunk type
u8 cf   # chunk flags
```
No length word is read at this level for LE streams — length, when needed, is
read by the individual per-category handler (see below) immediately after
`(ch, cf)`.

#### 2.4.1 Chunk type categories and dispatch

```
NJD_CN = 0     # "null" chunk — no-op, continue reading
NJD_CE = 255   # "end" chunk — terminates the chunk stream (or resumes a
               # deferred jump if one was queued by NJD_CB_DP; see below)

NJD_CB_BA=1, NJD_CB_DA=2, NJD_CB_EXP=3, NJD_CB_CP=4, NJD_CB_DP=5
  CHUNK_BITS = {1,2,3,4,5}     # "bits" pseudo-chunks (branch/jump control)

NJD_CT_TID=8, NJD_CT_TID2=9
  CHUNK_TINY = {8,9}           # texture-id chunks

NJD_CM_D=17, NJD_CM_A=18, NJD_CM_DA=19, NJD_CM_S=20,
NJD_CM_DS=21, NJD_CM_AS=22, NJD_CM_DAS=23
  CHUNK_MATERIAL = {17..23}    # material property chunks

NJD_CV_SH=32, NJD_CV_VN_SH=33, NJD_CV=34, NJD_CV_D8=35,
NJD_CV_UF=36, NJD_CV_NF=37, NJD_CV_S5=38, NJD_CV_S4=39, NJD_CV_IN=40,
NJD_CV_VN=41, NJD_CV_VN_D8=42, NJD_CV_VN_UF=43, NJD_CV_VN_NF=44,
NJD_CV_VN_S5=45, NJD_CV_VN_S4=46, NJD_CV_VN_IN=47,
NJD_CV_VNX=48, NJD_CV_VNX_D8=49, NJD_CV_VNX_UF=50
  CHUNK_VERTEX = {32..50}      # vertex chunks (see §2.4.3)

NJD_CO_P3=56, NJD_CO_P4=57, NJD_CO_ST=58
  CHUNK_VOLUME = {56,57,58}    # "volume"/strip-fan chunks (§2.4.5)

NJD_CS=64, NJD_CS_UVN=65, NJD_CS_UVH=66, NJD_CS_VN=67,
NJD_CS_UVN_VN=68, NJD_CS_UVH_VN=69, NJD_CS_D8=70,
NJD_CS_UVN_D8=71, NJD_CS_UVH_D8=72, NJD_CS_2=73,
NJD_CS_UVN2=74, NJD_CS_UVH2=75
  CHUNK_STRIP = {64..75}       # triangle-strip chunks (§2.4.6)
```

Dispatch loop (per chunk):
```
if ch == NJD_CE:
    if a deferred jump ("jump_to") is pending: seek there, clear it, continue
    else: stop reading chunks (end of stream)
elif ch == NJD_CN: continue (no-op)
elif ch in CHUNK_VERTEX: handle as vertex chunk
elif ch in CHUNK_BITS:   handle as bits/branch chunk
elif ch in CHUNK_MATERIAL: handle as material chunk
elif ch in CHUNK_TINY:    handle as texture-id chunk
elif ch in CHUNK_STRIP:   handle as strip chunk
elif ch in CHUNK_VOLUME:  handle as volume chunk, THEN STOP READING
                          (volume chunks terminate the stream — they
                          are treated as a final chunk type)
```

#### 2.4.2 Big-endian (GC) chunk header disambiguation — IMPORTANT QUIRK

When the same `NinjaChunkMixin` code parses **big-endian** payloads (GC `.nj`
files, or the GC-endianness NJCM inside a `.rel`), the header format changes:
for most "no-length" chunk categories (`NULL`, `END`, `BITS`, `TINY`,
`STRIP`, `VOLUME`, `MATERIAL`), the on-disk layout is a single big-endian
`u16` word whose **low byte is `ch`** and whose **high byte is `cf`**. But
for **VERTEX chunks**, the on-disk layout instead starts with a big-endian
`u16` **length word**, followed by a second big-endian `u16` `ch_cf` word
(low byte `ch`, high byte `cf`).

This creates a genuine ambiguity: reading the first u16 of a chunk, you don't
know a priori whether it's a `(ch,cf)` word (no-length chunk) or a length word
(vertex chunk) — because a vertex chunk's length word's **low byte** can
coincide with valid values for `TINY` (8–9), `BITS` (1–5), `STRIP` (64–75),
`VOLUME` (56–58), or `MATERIAL` (17–23) chunk types, or fall in `0x10–0x1F`.

**Disambiguation algorithm actually used (must be replicated exactly):**

```
read word0 (BE u16)
ch_cand = word0 & 0xFF
no_len = (ch_cand == 0 or ch_cand == 0xFF
          or ch_cand in BITS or ch_cand in TINY
          or ch_cand in STRIP or ch_cand in VOLUME
          or 0x10 <= ch_cand <= 0x1F)

if no_len and ch_cand not in (0, 0xFF):
    # ambiguous — could be a real no-len chunk header, OR the length
    # word of a vertex chunk whose low byte happens to collide.
    peek word1 (BE u16) without consuming permanently
    vc_ch = word1 & 0xFF
    if vc_ch in CHUNK_VERTEX:
        # candidate: word0 = vertex chunk length (in 4-byte words,
        # counting from just after the length field itself: see below),
        # word1 = the real ch_cf word for this vertex chunk.
        vsz = per-vertex byte size for vc_ch (lookup table, see §2.4.3)
        body_bytes = word0 * 4 - 4
        if body_bytes > 0 and body_bytes % vsz == 0:
            exp_vcount = body_bytes // vsz
            read next BE u16 (the position immediately after word1) as
                actual_vcount
            if actual_vcount == exp_vcount:
                CONFIRMED vertex chunk:
                  ch = vc_ch; cf = word1 >> 8
                  rewind stream to just before actual_vcount so the
                  vertex-chunk handler can read vcount itself
            else:
                false positive → rewind to just after word0;
                ch = ch_cand; cf = word0 >> 8   (treat as genuine no-len chunk)
        else:
            false positive → same fallback as above
    else:
        rewind to just after word0; ch = ch_cand; cf = word0 >> 8
elif no_len:
    # ch_cand is 0 or 0xFF — always unambiguous
    ch = ch_cand; cf = word0 >> 8
else:
    # word0 was definitely a length word (its low byte isn't any known
    # no-len type) — read the real ch_cf word next
    word1 = read BE u16
    ch = word1 & 0xFF; cf = word1 >> 8
```

The per-vertex-chunk-type byte-size lookup table (`_GC_VERTEX_SIZE`) used for
the `body_bytes % vsz` validation:

| Chunk type (decimal) | Name | Bytes/vertex |
|---|---|---|
| 32 | NJD_CV_SH | 16 |
| 33 | NJD_CV_VN_SH | 32 |
| 34 | NJD_CV | 12 |
| 35 | NJD_CV_D8 | 16 |
| 36 | NJD_CV_UF | 12 |
| 37 | NJD_CV_NF | 12 |
| 38 | NJD_CV_S5 | 12 |
| 39 | NJD_CV_S4 | 12 |
| 40 | NJD_CV_IN | 12 |
| 41 | NJD_CV_VN | 24 |
| 42 | NJD_CV_VN_D8 | 28 |
| 43 | NJD_CV_VN_UF | 24 |
| 44 | NJD_CV_VN_NF | 28 |
| 45 | NJD_CV_VN_S5 | 24 |
| 46 | NJD_CV_VN_S4 | 24 |
| 47 | NJD_CV_VN_IN | 24 |
| 48 | NJD_CV_VNX | 16 |
| 49 | NJD_CV_VNX_D8 | 20 |
| 50 | NJD_CV_VNX_UF | 16 |

Formula for the table: `12 (position) + 4 if "SH" padding + 12 if float
normal, or 4 if packed VNX normal + 4 if vertex color (D8) + 4 for NF's
nofs+padding field`.

#### 2.4.3 Vertex chunks (§ types 32–50)

**Header** (after the `(ch, cf)` disambiguation above):

- Little-endian: `u16 chunk_length_words` (discarded), `u16 vofs` (index
  offset — subsequent vertices are numbered starting at this base index),
  `u16 vcount`.
- Big-endian (GC): the length word was already consumed during
  disambiguation; what follows is `u16 vcount`, then `u16 vofs` (**note the
  reversed field order relative to the little-endian layout**).

**Per-vertex flags derived from `ch`:**
```
read_color  = ch in {NJD_CV_VN_D8, NJD_CV_VNX_D8, NJD_CV_D8}
read_normal = NJD_CV_VN(41) <= ch <= NJD_CV_VNX_UF(50)
is_sh       = ch in {NJD_CV_SH(32), NJD_CV_VN_SH(33)}
is_vnx      = ch in {NJD_CV_VNX(48), NJD_CV_VNX_D8(49), NJD_CV_VNX_UF(50)}
```

**Per-vertex layout** (read `vcount` times):
```
pos.x, pos.y, pos.z         : 3x f32                      (always)
[if is_sh]  w                : f32, discarded (SH4 padding = 1.0)
if is_vnx:
    packed : u32   # packed normal; decode:
        nx = ((packed >> 20) & 0x3FF) / 511.0 - 1.0
        ny = ((packed >> 10) & 0x3FF) / 511.0 - 1.0
        nz = ( packed        & 0x3FF) / 511.0 - 1.0
elif read_normal:
    norm.x, norm.y, norm.z   : 3x f32
    [if is_sh] w             : f32, discarded (SH4 padding = 0.0)
if read_color:
    b, g, r, a               : 4x u8, each /255.0   (file order is BGRA)
```

Special handling for `ch == NJD_CV_VN_NF (44)`: this chunk type appends a
4-byte field interpreted as `[nofs: i16, padding: i16]` **in little-endian
mode**, but because GC big-endian byte-swapping swaps the two 16-bit halves
of each 32-bit word, in **big-endian mode the on-disk order is
`[padding, nofs]`** — read padding first, discard it, then read `nofs`. The
resulting vertex is stored in the running vertex stack keyed by
`str(vofs + nofs)` **instead of** the usual `str(vofs + i)` key — this
lets an "NF" vertex chunk reference/patch a vertex slot defined by an earlier
chunk at a different index, used for skinning weight blending across bones.

**Quirk (NF vertex local-space fix):** `NinjaChunkMixin` NF-type vertices
must have their position/normal transformed via the **current node's own
matrix** (i.e. treat the position as being read and transformed exactly like
any other vertex chunk, using `self.current_matrix` at the time the NF chunk
is encountered) — NOT borrowed pre-transformed from whatever bone originally
defined vertex index `vofs+nofs`. Using a stale world-space value from a
sibling bone's stack entry causes mirrored-limb geometry (e.g. character arms)
to be positioned on the wrong side of the body. The correct approach stores
the NF vertex's own local-space-derived, current-bone-transformed position and
lets the render/patch step apply matrices at the point of use, not at bake
time.

Otherwise (non-NF chunk types), each vertex is stored in the stack keyed by
`str(vofs + i)` where `i` is the 0-based index within this chunk's `vcount`
run — this "vertex stack" is a persistent dict addressed by string index that
strip chunks later look up by index to build actual triangle geometry.

#### 2.4.4 Bits/branch chunks (types 1–5, `CHUNK_BITS`)

Used for save/jump control flow within the chunk stream (a limited branching
mechanism, presumably originally used to reuse shared vertex data across
multiple LOD or material variants):

- `NJD_CB_CP (4)` — "call/push": **stop reading the chunk stream immediately**
  and remember the current stream position, keyed by `cf` (0–255), in a
  256-slot table (`store_ofs[cf] = current_position`).
- `NJD_CB_DP (5)` — "return/pop": save the *current* position into a
  `jump_to` variable, then **seek to** `store_ofs[cf]` (the position
  previously saved for that same key). The next time an `NJD_CE` (end) chunk
  is encountered, instead of terminating, the stream jumps back to the saved
  `jump_to` position and clears it, resuming from where the deferred branch
  left off.
- `NJD_CB_BA (1)`, `NJD_CB_DA (2)`, `NJD_CB_EXP (3)` are recognized as
  member of the "bits" category (so `readChunks` dispatches them to the same
  handler) but the reference importer takes no action for them beyond the
  dispatch (they exist as documented NJD chunk types but are effectively
  no-ops here).

#### 2.4.5 Material chunks (types 17–23, `CHUNK_MATERIAL`)

```
u16 chunk_len_words     # present for both LE and BE streams, immediately after (ch,cf)
src = cf & 0x07
dst = (cf >> 3) & 0x07
if src == 1 and dst == 4: blendSrc = blendDst = 'ONE'      (additive blending)
elif src == 5 and dst == 4: blendSrc = blendDst = ''       (normal/default blending)
if ch & 0x01:  # diffuse color present
    b, g, r, a : 4x u8, /255.0   (BGRA byte order)
    diffuse = (r, g, b, a)
if ch & 0x02:  # ambient color present
    b, g, r    : 3x u8, /255.0   (BGR, with a discarded 4th byte)
    ambient = (r, g, b, 1.0)
if ch & 0x04:  # specular color present
    b, g, r    : 3x u8, /255.0   (BGR, with a discarded 4th byte)
    specular = (r, g, b, 1.0)
```
Note the chunk type's low 3 bits (`ch & 0x07`, i.e. whether it's `_D`, `_A`,
`_S` or a combination — the `NJD_CM_D/A/DA/S/DS/AS/DAS` variants) directly
gate which of the three color fields are present, in that fixed order.

#### 2.4.6 Texture-id (tiny) chunks (types 8–9, `CHUNK_TINY`)

```
u16 body
tex_id = body & 0x1FFF     # 13-bit texture index
material.texIndex = tex_id  # validated against loaded texture count at mesh-build time
```

#### 2.4.7 Volume chunks (types 56–58, `CHUNK_VOLUME`)

```
u16 chunk_len_words   # present for both LE and BE
u16 body
strip_count = body & 0x3FFF
for each of strip_count strips:
    i16 raw            # sign encodes winding: raw < 0 → clockwise
    slen = abs(raw)
    for slen times: u16 vertex_index   (no UV — volume/point chunks carry no UV data)
```
Triangles are built from each strip's point list using standard triangle-strip
alternating winding (see §2.4.9). **Volume chunks terminate the chunk stream**
after being processed (`readChunks` returns immediately after dispatching to
the volume handler — no further chunks in this stream are read).

#### 2.4.8 Strip chunks (types 64–75, `CHUNK_STRIP`)

```
u16 chunk_len_words   # present for both LE and BE
u16 body
double_side = cf & 0x10
strip_count = body & 0x3FFF
user_offset = body >> 14    # extra per-vertex user-data words to skip, if any
for each of strip_count strips:
    i16 raw          # sign encodes winding: raw < 0 → clockwise
    slen = abs(raw)
    for k in range(slen):
        u16 vertex_index
        if ch == NJD_CS_UVN (65):
            i16 u, i16 v   (each /255.0; v is flipped: v_final = 1.0 - v)
        elif ch == NJD_CS_UVH (66):
            i16 u, i16 v   (each /1023.0; v is flipped the same way)
        # (other CS_* variants carry no UV in this implementation)
        if k > 1 and user_offset:
            skip (user_offset * 2) bytes of unspecified per-vertex user data
```

Triangle winding for strip chunks (note: **opposite convention from volume
chunks** — cw flag flips per triangle here, unconditionally, starting `cw =
False` for every strip):
```
for k in range(slen - 2):
    if cw: (a,b,c) = (strip[k], strip[k+2], strip[k+1])
    else:  (a,b,c) = (strip[k+1], strip[k+2], strip[k])
    cw = not cw
    emit triangle (a,b,c)
    if double_side: also emit triangle (b,a,c)   # reversed winding, same 3 verts
```

#### 2.4.9 Assembling final mesh data (`_appendPoints`)

Given a flat list of `(index, uv)` point references (built by strip or volume
handlers above), triangles are grouped 3-at-a-time. Any triangle where **any**
of its 3 vertex indices is missing from the current vertex stack is **skipped
entirely** (all 3, atomically — never emit a partial/degenerate triangle).
Surviving triangles' positions, normals (if present on that vertex), vertex
colors (if present), and UVs (if present on that *point*, since UV lives on
the strip point, not the vertex-stack entry) are appended to flat per-mesh
arrays, and a new mesh entry is appended to `meshes_data`, tagged with the
current bone index and a de-duplicated material index (materials are
deduplicated by a key tuple of `(diffuse, texIndex, blendSrc, blendDst,
has_vertex_colors)` — note `has_vertex_colors` is part of the key precisely
so that meshes with and without vertex-color data never accidentally share one
material, which would otherwise force a "Col" attribute lookup on a mesh that
has none).

### 2.5 POF0 relocation table (used when present)

**Purpose:** some NJ files (particularly certain GC "boss" models rebuilt from
a non-zero link/serialization base) store pointer fields (childOfs, meshOfs,
etc.) as **absolute addresses that include an unknown constant base offset**,
rather than offsets relative to the start of the NJCM payload. A `POF0` chunk,
when present, lists exactly which 32-bit words within a target payload are
pointer fields, so the base can be inferred and subtracted.

**POF0 payload encoding** — a variable-length difference-coded list of byte
offsets (each entry describes how far to advance from the previous listed
offset; the list is terminated by a `0x00` byte):

```
current = 0
loop:
    b = next byte
    if b == 0x00: END OF LIST
    top2 = b >> 6                # top 2 bits select encoding width
    if top2 == 0b00:              delta = b * 4                                    (1-byte form, b in 0x01..0x3F)
    elif top2 == 0b01:            delta = ((b & 0x3F) << 8 | next_byte) * 4         (2-byte form)
    elif top2 == 0b10:            delta = ((b & 0x3F) << 16 | next_byte << 8 | next_byte) * 4  (3-byte form)
    else (0b11, i.e. 0xC0+):      unused by PSO — stop
    current += delta
    emit current as a pointer-field byte-offset
```

**Applying the relocation** (`apply_pof0_relocation`): for every offset in the
decoded list, read the u32 pointer value stored there in the target payload
(NJCM bytes). If every non-zero pointer value is already `< payload_size`, no
relocation is needed — return the payload unmodified. Otherwise:

1. Split pointer values into `invalid` (`>= payload_size`) and `valid`
   (`< payload_size`) sets.
2. Solve for a base `B` such that subtracting it from every `invalid` pointer
   brings it into `(0, payload_size)`, while also keeping every `valid`
   pointer (which may also include the same base, coincidentally still
   in-range) positive after subtraction:
   ```
   b_lo = round_down_to_4((max(invalid) - payload_size) + 4)
   b_hi = round_down_to_4(min(invalid) - 4)
   if valid: b_hi = min(b_hi, round_down_to_4(min(valid) - 4))
   ```
   If `b_lo > b_hi`, the pointer range is too wide to be a simple
   base-offset scheme (likely the POF0 table encodes absolute *file* offsets
   rather than NJCM-relative ones) — abort relocation, leave the payload
   unmodified.
3. **Structural validation**: candidate `B` values are only accepted if the
   resulting *adjusted* pointer targets "look like" the start of a valid NJ
   bone node — specifically, the u32 at the adjusted offset must be
   `<= 0x3FFF` (a plausible small flags word). The root bone is always at
   NJCM offset 0, so its `childOfs` (raw file offset 44) and `siblingOfs`
   (offset 48) fields are used as anchor samples when present in the POF0
   table (falling back to the first 5 invalid pointer values otherwise).
4. Iterate candidate "true minimum offsets" for the smallest invalid pointer
   (`true_min` from 4 up to `min(4097, payload_size)`, stepping by 4),
   compute `B = min_inv - true_min`, and accept the first `B` in
   `[b_lo, b_hi]` that (a) keeps all invalid pointers in-range after
   subtraction, (b) keeps all valid pointers positive, and (c) passes the
   bone-node structural check on the anchor samples. If no candidate passes
   the structural check, a relaxed second pass accepts any `B` satisfying
   only (a) and (b) — but a payload rewritten using only this relaxed pass is
   considered untrustworthy and the relocation is **not applied** (the
   original payload is returned unmodified) to avoid corrupting strip data.
5. If a validated `B` is found, patch every non-zero pointer at every listed
   offset: `new_value = old_value - B`; skip (leave unpatched, but log a
   warning) any pointer whose adjusted value still falls outside
   `(0, payload_size)`.

**Which POF0 chunk to use, when multiple exist:** some files contain two
`POF0` chunks — one appearing *before* the NJCM chunk (which relocates NJTL
texture-list pointers, not NJCM pointers) and one appearing *after* the NJCM
chunk (which relocates the NJCM bone/mesh pointers). Relocation should **only
ever be applied using a POF0 chunk that appears after the NJCM chunk in file
order**; a before-NJCM POF0 must never be applied to the NJCM payload.
Additionally, relocation should be skipped entirely if the root bone node's
pointer fields already look valid (i.e. `flags <= 0x3FFF` and childOfs/
meshOfs/siblingOfs values are either 0 or already `< payload_size`) — most DC
and BB `.nj` files need no relocation at all; only a minority of GC-sourced
models (with a non-zero serialization base) require it.

### 2.6 Mesh header (as pointed to by a bone node's `meshOfs`)

```
u32 vofs   # offset (from NJCM payload start) to the "vertex chunk stream" for this mesh — the chunk stream containing vertex chunks + material/texture chunks that DEFINE vertex data
u32 cofs   # offset to the "polygon chunk stream" for this mesh — contains strip/volume chunks that CONSUME the vertex data defined via vofs to emit actual triangles
[12 bytes: center.x, center.y, center.z as 3 floats, then radius as 1 float — 16 bytes total, all skipped/unused by the geometry importer]
```
If `vofs != 0` and in-bounds, seek there and run `readChunks` (this populates
the vertex stack and material state). If `cofs != 0` and in-bounds, seek there
and run `readChunks` again (this consumes strip/volume chunks referencing the
vertex stack populated by the `vofs` pass, and appends actual mesh geometry).
Both streams share the same running `vertex_stack` and `material` state on the
importer instance.

---

## 3. XJ — Blue Burst / PC Ninja Model Format

**Overview:** `.xj` is the PC/Blue Burst evolution of the NJ chunk-based
format described in Section 2. It reuses the same top-level `NJTL` / `NJCM` /
`NMDM` magic chunk structure and a very similar node/bone tree layout, but
**does not use the NJD chunk-type dispatch system at all** — instead, each
mesh has a single fixed, simpler binary layout (no per-chunk-type variability,
no BITS/branch mechanism, no POF0 relocation support, always little-endian).

### 3.1 Top-level parse

Identical outer loop to NJ: scan magics (`NJTL`, `NJCM`, `NMDM`, or unknown)
at the top level, each followed by a `u32` payload length. `NJTL` is parsed
into `texNames` (identical layout to §2.2, i.e. list_ofs/count table, then
per-entry `u32 name_ofs + 8 skipped bytes`, then read strings). `NJCM` is
parsed via `readBone` (below). `NMDM` is currently skipped entirely (read and
discarded).

### 3.2 Bone/node layout (`readBone`)

**Byte-for-byte identical 52-byte layout to the NJ standard (non-quaternion)
node** described in §2.3 — `flags(u32), meshOfs(u32), pos(3xf32),
rot(3x BAMS i32), scl(3xf32), childOfs(u32), siblingOfs(u32)`. XJ has **no
quaternion node variant** — the `flags & 0x400` special case from NJ does not
apply here; XJ always uses BAMS euler rotation. Flags bits `0x01`
(skip-translate) and `0x02` (skip-rotate) behave identically to NJ. Traversal
(mesh → child → sibling, matrix composition, bail-out on out-of-bounds
pointers) is identical to NJ's `readBone` (§2.3), and the node's DFS index is
likewise recorded for armature/animation bone indexing.

### 3.3 Mesh layout — the key difference from NJ

```
u32 flags
u32 vertex_info_list_offset
u32 vertex_info_count
u32 triangle_strip_list_a_offset
u32 triangle_strip_a_count
u32 triangle_strip_list_b_offset
u32 triangle_strip_b_count
f32 center.x, f32 center.y, f32 center.z
f32 radius
```

This is a completely different scheme from NJ's chunk-stream mesh format: XJ
describes vertex data via an **array of vertex-info descriptors** (see below)
rather than a stream of typed vertex chunks, and describes polygons via two
separate strip lists ("A" and "B" — list B typically used for alpha/blended
material passes, though the importer does not otherwise distinguish them
except for tracking which list they came from).

#### 3.3.1 Vertex info entries (16 bytes each) — CRITICAL multi-entry quirk

Starting at `vertex_info_list_offset`, there are `vertex_info_count` entries,
each exactly **16 bytes**:

```
offset  size  field    meaning
0       2     vtype    u16 bitmask: bit0=has UV, bit1=has normal, bit2=has color
2       2     unk      u16, unused
4       4     vofs     u32, absolute offset (from NJCM payload start) to this entry's vertex array
8       4     vsz      u32, per-vertex byte size for this entry (unused by the parser — recomputed from vtype instead)
12      4     vcnt     u32, vertex count in this entry
```

**QUIRK — vertex_info_count can be greater than 1, and this is normal, not an
error.** A single mesh may have multiple vertex-info entries describing
*overlapping* vertex index ranges, where an earlier entry supplies UV data and
a later entry (often added for a secondary skinning/LOD pass) does NOT carry
UV. **The fix required: loop over ALL vertex_info_count entries in order, and
when writing a vertex at a given index, only OVERWRITE fields the current
entry actually provides — never null out a `uv` value that was set by an
earlier entry just because the current entry lacks one.** Concretely: for
vertex index `i`, initialize `vertex.uv = existing_vertex.uv if
existing_vertex else None` before applying the new entry's fields — i.e.
carry forward a previously-set UV across entries.

**Per-vertex layout within each vertex-info entry**, read `vcnt` times in
order starting at `vofs`:
```
pos.x, pos.y, pos.z : 3x f32                (always; transformed by current_matrix.transformPoint)
if vtype & 0x02 (normal):
    norm.x, norm.y, norm.z : 3x f32          (transformed by current_matrix.transformNormal)
if vtype & 0x04 (color):
    r, g, b, a : 4x u8, /255.0               (RGBA byte order — NOTE: opposite of NJ's BGRA order)
if vtype & 0x01 (uv):
    u, v : 2x f32
    stored uv = (u, 1.0 - v)                 (V flipped for Blender's bottom-left origin)
```

#### 3.3.2 Strip lists (A and B)

For each of `triangle_strip_a_count` / `triangle_strip_b_count`, a strip
descriptor:
```
u32 material_property_list_offset
u32 material_property_list_size
u32 index_list_offset
u32 index_count
u32 unknown
```
For each strip descriptor: seek to `material_property_list_offset` and read
`material_property_list_size` material properties (§3.3.3), then seek to
`index_list_offset` and read `index_count` signed 16-bit vertex indices.

**Triangle winding for XJ strips — always double-sided (both windings
emitted):**
```
for i in range(len(points) - 2):
    a, b, c = points[i], points[i+1], points[i+2]
    if a == b or b == c or c == a: continue    # skip degenerate
    emit triangle (a, b, c)
    emit triangle (a, c, b)                    # reversed winding, same verts
```
This differs from NJ's strip winding, which flips the winding direction on
alternating indices instead of emitting both windings for every triangle.

#### 3.3.3 XJ material properties

For each of `prop_count` properties, read a `u32 mat_type` then:
```
mat_type == 2:  # blend mode
    u32 dst, u32 src
    if src==1 and dst==4: blendSrc=blendDst='ONE'
    elif src==5 and dst==4: blendSrc=blendDst=''
    [4 bytes skipped]
mat_type == 3:  # texture index
    u32 tex_id  → material.texIndex
    [8 bytes skipped]
mat_type == 5:  # diffuse color
    u8 r,g,b,a  (each /255.0, RGBA order)
    [8 bytes skipped]
else: [12 bytes skipped]  (unrecognized property types are consumed but ignored)
```
Note XJ has **no `mat_type == 4` (double-sided flag)** handling — unlike the
NJ chunk material format, XJ materials are never marked double-sided by the
parser.

### 3.4 Mesh assembly

Identical de-duplication/merge strategy to NJ's `_appendPoints` (§2.4.9):
materials are keyed by `(diffuse, texIndex, blendSrc, blendDst,
has_vertex_colors)`; per-vertex bone index (the DFS index of the bone whose
mesh this triangle belongs to) is tracked per-vertex in XJ (unlike NJ's
single per-mesh `bone_index`), because a single XJ mesh entry only ever
belongs to one bone node's local vertex list, but downstream code stores it as
a `bone_indices` list per-vertex for generality with rigged models.

---

## 4. GJ — GameCube Flipper Model Format

**Overview:** `.gj` is the GameCube ("Flipper" GPU) model format used in PSO
Episode I & II (GameCube) and Episode III (Card Revolution). Its outer chunk
structure again reuses the `GJTL`/`GJCM` names (analogous to NJTL/NJCM) but
**every byte inside a GJCM chunk (and inside GJTL, when read) is
big-endian**, matching PowerPC hardware endianness. The outer chunk-magic
scan itself is little-endian (matching how the 4-byte ASCII tags are stored
regardless of payload endianness).

### 4.1 Top-level parse

```
loop over the file:
    u32 magic (LE)
    if magic == GJTL:
        u32 chunk_len (LE)
        payload = read chunk_len bytes
        parse payload as a big-endian stream → _readTexList
    elif magic == GJCM:
        u32 chunk_len (LE)
        record file_ofs = current position (absolute offset into the outer file)
        payload = read chunk_len bytes
        parse payload as a big-endian stream → _readNode (root node)
        STOP (only the first GJCM chunk is treated as the geometry root)
    else:
        skip: read u32 length (LE), skip that many bytes
```

### 4.2 GJTL — texture name list (`_readTexList`)

Identical table structure to NJTL (§2.2) but all fields big-endian:
```
u32 list_ofs
u32 count
seek to list_ofs; for count entries: u32 name_ofs, then skip 8 bytes
for each name_ofs: seek there, read null-terminated ASCII string
```

**Texture remapping quirk:** if an external texture archive was already
loaded (e.g. from a separate `.gvm`/`.xvm` sidecar file) AND the GJTL name
list is non-empty, the importer attempts to **remap** the loaded texture list
so that index 0 corresponds to `texNames[0]`, index 1 to `texNames[1]`, etc.,
by matching texture names case-insensitively. This remap is only performed if
at least one loaded texture has a "real" (non-generic) name — i.e. not
matching the auto-generated `texture_NNN` pattern the GVM/PVM loaders assign
when no name table exists in the archive itself. If a name from GJTL has no
match in the loaded archive, a placeholder 8x8 fully-transparent black texture
is substituted so indices stay aligned. If the loaded texture set only has
generic names (no archive-level name table was present), **no remap is
performed** and textures are used in direct index order.

### 4.3 Node hierarchy (`_readNode`)

Layout is analogous to NJ's node (§2.3) but with big-endian fields, no
quaternion variant, and always exactly 52 bytes:

```
offset  size  field        encoding
0       4     flags        u32 BE
4       4     meshOfs      u32 BE
8       4     pos.x/.y/.z  3x f32 BE
20      4     rot.x/.y/.z  3x i32 BE (BAMS → radians via same 2π/65536 constant)
32      4     scl.x/.y/.z  3x f32 BE
44      4     childOfs     u32 BE
48      4     siblingOfs   u32 BE
```

Transform build order and flag semantics are identical to NJ (§1.4, §2.3):
`rotate()` unless `flags & 0x02`, then `translate()` unless `flags & 0x01`,
then compose with parent. Node DFS order is recorded the same way for
armature building. Traversal is mesh → child (with `pNode = {matrix, idx}`
passed as the new parent) → sibling (with the *original* parent passed
through unchanged) — same first-child/next-sibling recursive-descent
pattern as NJ, except bounds checks here use `<=` (permit pointer value equal
to stream size) rather than NJ's `>=` (which rejects it) — a small but real
difference in the two parsers' edge-of-buffer tolerance.

### 4.4 Mesh header

```
u32 prop_ofs        # offset to vertex-attribute property table
u32 (zero, unused/reserved)
u32 strip_ofs       # offset to primary ("opaque") strip-group table
u32 astrip_ofs      # offset to secondary ("alpha"/translucent) strip-group table
i16 strip_count
i16 astrip_count
[16 bytes: center xyz (3 floats) + radius (1 float), skipped]
```
If `prop_ofs != 0`, seek there and read vertex-attribute properties
(`_readProps`, §4.5) — this populates flat position/normal/color/uv arrays for
the mesh. If `strip_count != 0`, seek `strip_ofs` and read that many strip
groups as non-alpha; if `astrip_count != 0`, seek `astrip_ofs` and read that
many strip groups flagged alpha (the alpha flag isn't otherwise used
differently by the parser beyond bookkeeping).

### 4.5 Vertex attribute properties (`_readProps`) — GX vertex format table

```
loop:
    u8 type
    if type == 0xFF: break (end of attribute list)
    u8  size
    u16 count
    u32 unknown
    u32 offset    # absolute offset (from GJCM payload start) to this attribute's raw data
    u32 length    # byte length of this attribute's raw data
```

Attribute `type` values recognized:
| type | meaning | per-element layout |
|---|---|---|
| 1 | POS | 3x f32 BE → transformed by `current_matrix.transformPoint` |
| 2 | NORM | 3x f32 BE → transformed by `current_matrix.transformNormal` |
| 3 | COLOR | 1x u32 BE, packed **RGBA** in that byte order (`R=bits31-24, G=23-16, B=15-8, A=7-0`), each channel /255.0 |
| 5 | UV | 2x i16 BE, each /255.0; V flipped (`v_final = 1.0 - v`) |
| other | unrecognized — skip `size` bytes per element (structural placeholder, contents ignored) |

For each attribute, seek to `offset` and read `count` elements sequentially
into the appropriate flat array (`self._pos`, `self._norm`, `self._color`,
`self._uv`).

### 4.6 Strip groups and GX vertex descriptor / index decoding

Each strip-group table entry (read `strip_count` or `astrip_count` times):
```
u32 mat_ofs      # offset to material property block
u32 mat_count    # number of material properties
u32 strip_ofs    # offset to the packed primitive/index stream
u32 strip_len    # byte length of that stream
```
For each entry: seek `mat_ofs`, read `mat_count` material properties
(`_readGCMat`, §4.7); seek `strip_ofs`, read `strip_len` bytes of packed
primitive data (`_readIndices`, §4.8).

#### 4.7 GC material properties (`_readGCMat`)

```
for count times:
    u8  t
    [3 bytes skipped/padding]
    u32 val
    if t == 0x01: face_flags = val    # winding/culling flags, consumed as GX "primitive format" state
    elif t == 0x08:
        tid = val & 0x1FFF            # 13-bit texture index, same masking convention as NJ's tiny chunk
        tex_count = len(texNames) if texNames else len(textures)
        material.texIndex = tid if tid < tex_count else -1
```

**Quirk (GC BML texture index fix):** when a `.gj` model has been extracted
from a BML archive (§5) with **no accompanying `GJTL` chunk** (texture name
list is empty because BML-extracted GJ payloads commonly omit it), the
`tex_count` guard above must fall back to `len(self.textures)` — i.e. the
**absolute index into the loaded GVM texture archive** — rather than
`len(self.texNames)` (which would be 0, causing every texture reference to be
rejected as out-of-range). When a `GJTL` **is** present, `tid` is a
GJTL-relative index (validated and remapped per §4.2); when it is absent,
`tid` must be treated as a raw absolute GVM archive index instead.

#### 4.8 GX vertex descriptor and packed index stream (`_readIndices`) — CRITICAL QUIRK

`face_flags` (`ff`, set by material type `0x01` above) encodes, **per
attribute, independently**, both (a) whether that attribute is present at all
in the index stream and (b) whether its index is 8-bit or 16-bit:

```
bit 0x8    (bit 3)  : position attribute present
bit 0x4    (bit 2)  : position index width — 1 = 16-bit, 0 = 8-bit
bit 0x20   (bit 5)  : normal attribute present
bit 0x10   (bit 4)  : normal index width — 1 = 16-bit, 0 = 8-bit
bit 0x80   (bit 7)  : color attribute present
bit 0x40   (bit 6)  : color index width — 1 = 16-bit, 0 = 8-bit
bit 0x800  (bit 11) : UV attribute present
bit 0x400  (bit 10) : UV index width — 1 = 16-bit, 0 = 8-bit
```

**QUIRK (this is a previously-fixed real bug — document carefully): each
attribute's index-width bit is entirely independent of the others.** It is
*not* a single shared "all indices are 8-bit" or "all indices are 16-bit"
flag for the whole vertex — position could use an 8-bit index while UV
simultaneously uses a 16-bit index in the very same strip. **Each attribute's
width bit must be read and applied only to that attribute's own index reads**,
never assumed shared across attributes. The correct format table is built as
an ordered list of `(channel_name, index_byte_width)` pairs, checked in
strict order pos → norm → color → uv, each independently gated by its own
presence bit and sized by its own width bit:
```
fmt = []
if ff & 0x8:   fmt.append(('pos',   2 if (ff & 0x04)  else 1))
if ff & 0x20:  fmt.append(('norm',  2 if (ff & 0x10)  else 1))
if ff & 0x80:  fmt.append(('color', 2 if (ff & 0x40)  else 1))
if ff & 0x800: fmt.append(('uv',    2 if (ff & 0x400) else 1))
```

**Primitive stream format** (GX display-list-style packed primitives), read
until `strip_len` bytes have been consumed:
```
loop while bytes remain in this strip's byte range:
    u8 prim
    if prim == 0x00: continue (padding byte between primitives)
    if prim not in (0x90, 0x98): ABORT parsing this and all subsequent strips in the mesh (unrecoverable format error / unsupported primitive)
    u16 vertex_count
    for vertex_count times:
        for (channel, index_width) in fmt (in the fixed pos/norm/color/uv order above):
            index = read u16 if index_width==2 else read u8
            look up index into the corresponding flat array populated in §4.5
              (self._pos[index], self._norm[index], self._color[index], self._uv[index])
              — out-of-range indices are silently dropped (that channel is simply absent
              for this vertex, rather than raising an error)
```

`prim == 0x98` is a **triangle fan**: emit triangles using alternating winding
across the fan (`cw` toggles starting `True`):
```
cw = True
for i in range(fan_len - 2):
    if cw: (a,b,c) = (i, i+2, i+1)
    else:  (a,b,c) = (i+1, i+2, i)
    cw = not cw
    emit (a + running_offset, b + running_offset, c + running_offset)
```
`prim == 0x90` is a **triangle list** (NOT a strip, despite occupying a
"strip" table): every 3 consecutive vertices form one triangle with a fixed
winding swap:
```
for i in range(fan_len // 3):
    emit (3*i + offset, 3*i+2 + offset, 3*i+1 + offset)
```
`running_offset` accumulates the number of vertices emitted so far across all
primitives within this one strip-group entry, since indices for triangle
assembly are local to the flattened per-strip-group vertex buffer (not the
original attribute arrays).

### 4.9 GC stage parsing (`parse_stage`) — standard Flipper .rel

See Section 7.3 for the standard GC stage `n.rel` format, and Section 7.4 for
the Episode III Card Revolution variant.

---

## 5. BML — Multi-Model Archive

**Overview:** `.bml` is a container archive bundling multiple models (`.nj` /
`.gj` / `.xj`), their paired texture archives (`.pvm` / `.gvm`), and animation
files (`.njm`) into a single file, compressed per-entry with PRS (§1.5). Used
on both DC and GC (endianness auto-detected per-archive).

### 5.1 Header and endianness detection

```
offset 0x00-0x03 : unknown / reserved (not validated)
offset 0x04      : count field, read as BOTH a big-endian i32 and a
                   little-endian i32; whichever interpretation yields
                   a "sane" value (0 < count <= 2000) is used. If BOTH
                   interpretations look sane, big-endian is preferred
                   (GC-format archives are the ambiguous default).
```

### 5.2 Entry table (starts at file offset `0x40`)

Each table slot is **0x40 (64) bytes**, read using the detected byte order:
```
offset  size  field
0x00    32    name          (ASCII, space/tab/CR/LF/NUL-trimmed from the right)
0x20    4     compressed_size    (u32)
0x24    4     unknown (u32, skipped)
0x28    4     decompressed_size  (u32)
0x2C    4     pvm_comp           (u32 — size of an embedded, PRS-compressed texture archive immediately following this model in the data stream; 0 if none)
0x30    4     pvm_decomp         (u32 — decompressed size of that embedded texture archive)
[0x34..0x40 : 12 bytes of padding, not otherwise used]
```
(In code terms, this is `struct.unpack(bo+'32sIIIII', slot)` — 5 little/big
u32 fields after the 32-byte name: `compressed_size`, an unnamed field, then
`decompressed_size`, `pvm_comp`, `pvm_decomp`.)

For each entry, if `pvm_comp != 0`, a **synthetic second archive entry** is
inserted into the result list immediately after the model entry: its filename
is the model's name with its extension replaced by `.gvm` (if the archive is
big-endian, i.e. GameCube) or `.pvm` (if little-endian, i.e. Dreamcast), and
its compressed/decompressed sizes come from the `pvm_comp`/`pvm_decomp`
fields. This is how an embedded, per-model texture archive is represented
alongside its model in the flat `entries` list that `bml_read` returns.

### 5.3 Compressed data region and alignment

The compressed payload region begins at the **next 0x800-byte-aligned offset
at or after the end of the entry table**:
```
ofs = (table_end_position + 0x7FF) & 0xFFFFF800
```
(If `table_end_position` is already a multiple of `0x800`, it is used as-is —
no extra padding block is added; this is a rounding-up operation, not
"always advance to the next boundary".)

Then, for each entry (model or synthetic texture entry) in table order:
1. Skip forward past any `0x00` padding bytes at the current read position.
2. Read exactly `compressed_size` bytes as this entry's raw (PRS-compressed) payload.
3. Advance the read position by `compressed_size`.
4. Run `decompress_prs` (§1.5) on the raw bytes to get the final decompressed data (on any exception, substitute an empty byte string rather than aborting the whole archive read).

### 5.4 Model/texture pairing at the importer level

See Section 10 for the full multi-strategy texture-association logic used
when importing a BML's contents (embedded pairing, per-model sidecar files,
BML-level shared sidecar, and LOD/variant texture reuse).

---

## 6. NJM — Ninja Motion / Animation Format

**Overview:** `.njm` files store keyframed bone animation data ("motions")
applied to a previously-imported NJ/XJ/GJ bone hierarchy. Two container
variants exist:

- **NMDM / "v2" variant** (native DC/GC files): begins directly with the
  `NMDM` magic (§1.1) followed by a `u32` chunk size, after which the "motion
  header" begins immediately.
- **BB "player format" variant**: no `NMDM` magic at the start. Instead, a
  16-byte footer at the very end of the file holds an indirection chain:
  read `u32 offset1` from the last 16 bytes, seek to `offset1` and read
  `u32 action_offset`, seek to `action_offset + 4` and read `u32
  motion_start` — that final value is the absolute file offset where the
  motion header begins. BB files are always little-endian at this
  indirection-chain level (endianness of the motion header itself is still
  separately auto-detected, see below, since some BB-format files are
  reportedly still able to embed big-endian motion payloads).

### 6.1 Endianness auto-detection (motion_start sanity check)

Once `motion_start` is known (by either path above), the first field at that
offset — `m_data_table_rel`, a signed 32-bit "offset to the per-bone table,
relative to `motion_start`" — is read under **both** little-endian and
big-endian interpretation. Whichever interpretation yields a value in the
plausible range `4 <= v <= 4096` is selected as the byte order for the entire
motion header and all its keyframe/track data. If **neither** interpretation
falls in that range, the file is considered unparseable and parsing aborts
(`parse_njm` returns `None`).

### 6.2 Motion header

At `motion_start`, using the detected byte order:
```
i32 m_data_table_rel   # relative offset (from motion_start) to the per-bone track-offset table
i32 frame_count        # total animation length in frames
u16 motion_type        # bitmask of which channels are present, see below
u16 inp_fn             # low byte = interpolation type; see below
```

**`motion_type` bit flags** (which per-bone channels exist in this file, and
in what fixed column order they'll appear in the per-bone table):
```
bit 0x0001  : position channel present   → channel order slot: 1st if present
bit 0x0002  : euler-angle channel present → 2nd if present
bit 0x2000  : quaternion channel present  → 3rd if present (after euler)
bit 0x0004  : scale channel present       → 4th (last) if present
```
The active channel names, filtered by these bits and always emitted in the
fixed relative order **position → euler → quaternion → scale** (skipping any
that aren't flagged), form the `channels` list; `num_channels = len(channels)`.
If no channel bit is set at all, the file is considered invalid (`None`).

`interp = inp_fn & 0xFF` — only the low byte matters; `0` = Linear
interpolation, `1` = Bezier/"Spline" interpolation for the resulting Blender
F-Curve keyframes. (The high byte of `inp_fn` is not used for anything by
this importer — notably, the **node/bone count is NOT stored anywhere
explicit in the header**; it must be inferred, see §6.3.)

### 6.3 Per-bone track-offset table — inferring `element_count`

The table itself has no explicit stored length; its size (`element_count`,
i.e. number of bones/tracks) must be inferred by finding where the table ends
and keyframe payload data begins. The table lives at absolute offset
`table_abs = motion_start + m_data_table_rel`. Each table row ("bone slot") is
`bytes_per_bone = num_channels * 8` bytes (this is a **struct-of-arrays**
layout: for each bone slot, **all** channel *offsets* come first, THEN all
channel *counts*):
```
row layout (for num_channels == N):
  i32 offset[0], i32 offset[1], ..., i32 offset[N-1],   (N x 4 bytes)
  i32 count[0],  i32 count[1],  ..., i32 count[N-1]      (N x 4 bytes)
```

To infer the number of rows: scan up to 512 candidate row indices `i`
starting at row 0. For each row, for each channel `j` in that row, if
`count[j] > 0` AND `offset[j] >= m_data_table_rel + (i+1) * bytes_per_bone`
(i.e. the keyframe data this row claims to point at lies strictly *after* the
end of this row within the table region — a sanity filter that rejects
misreading leftover keyframe-payload bytes as if they were still table
entries), track the minimum such `offset[j]` value seen across the whole
scan, `min_kf_off`. Once the scan completes (or 512 rows are exhausted, or
data runs out), if no valid `min_kf_off` was found, the file is unparseable.
Otherwise:
```
element_count = (min_kf_off - m_data_table_rel) // bytes_per_bone
```
This is exactly the number of complete `bytes_per_bone`-sized rows that fit
before the first real keyframe payload begins — i.e., the bone/track count.
If `element_count <= 0`, the file is unparseable.

### 6.4 Per-bone keyframe channels

For each `bone_idx` in `range(element_count)`, read its table row
(`ch_offsets[0..N-1]` then `ch_counts[0..N-1]`, per the row layout in §6.3).
For each channel present, if its count is `> 0` and its absolute data offset
(`motion_start + ch_offsets[i]`) is within the file, decode keyframes as
follows:

**Position channel** — 16 bytes/keyframe, always wide-format:
```
i32 frame
f32 x, f32 y, f32 z
```

**Euler channel** — variable per-keyframe size, auto-detected per-track as
either **compact (8 bytes)** or **wide (16 bytes)**:
```
detection: read the first keyframe's frame field as an unsigned 16-bit value;
if that value is >= frame_count, this track cannot be compact-encoded → wide.
Otherwise, tentatively assume compact and verify by reading up to the first
8 keyframes' frame fields (each as u16, at stride 8 bytes) and confirming
they are monotonically non-decreasing; if any decrease is found, fall back
to wide encoding for the WHOLE track.

compact (8 bytes/keyframe):
    u16 frame
    u16 rx, u16 ry, u16 rz     (each BAMS-style: * 2*PI/65536 → radians)
wide (16 bytes/keyframe):
    i32 frame
    i32 rx, i32 ry, i32 rz     (each BAMS-style: * 2*PI/65536 → radians)
```

**Quaternion channel** — 20 bytes/keyframe:
```
i32 frame
f32 w, f32 x, f32 y, f32 z    (note: W FIRST, then X,Y,Z)
```

**Scale channel** — 16 bytes/keyframe:
```
i32 frame
f32 x, f32 y, f32 z
```

### 6.5 Retargeting to a Blender armature / rest pose

Once decoded, `parse_njm`'s output (`frame_count`, `interp`, `channels`,
`tracks`, `element_count`) is converted into pose-bone F-Curves as follows
(the general technique, without Blender-API-specific details, is the useful
part for any independent implementation targeting a different engine):

1. **Bone matching by name similarity**: the target armature is chosen from
   all previously-imported armatures by computing, for each candidate
   armature, `score = 2 * longest_common_substring(action_filename,
   armature_name_stem) + (1 if armature_bone_count == element_count else 0)`
   (case-insensitive substring match), and picking the highest-scoring
   armature. This heuristic — rather than an explicit filename convention —
   is what lets an animation file named anything resembling the model's name
   bind to the correct skeleton, with bone-count equality as a tiebreaker
   nudge.
2. **Rest-pose delta encoding**: each bone's NJM keyframes store **absolute**
   local transforms (not deltas from a rest pose), matching the same
   local-transform semantics as the bone's own NJ/XJ/GJ node record (position
   and BAMS/quaternion rotation, before matrix composition with the parent).
   To convert to a Blender-style rest-relative pose-bone transform:
   - Build a 3x3 rotation matrix `R_rest` from the bone's original rest-pose
     euler angles using the same **Rz · Ry · Rx composition order** as
     `DashMat4.rotate()` (i.e. matching NJ's own X-then-Y-then-Z rotation
     application order — see §1.4).
   - For **position** keyframes: `delta = keyframe_pos - rest_pos` (a plain
     vector subtraction in the bone's local space), then
     `posed_position = R_rest_transpose * delta` (i.e. un-rotate the raw
     positional delta into the bone's own rest-oriented local frame, since
     `R_rest_transpose == R_rest_inverse` for a pure rotation matrix).
   - For **euler** keyframes: build `R_njm` from the keyframe's raw euler
     angles (same Rz·Ry·Rx convention), then compute
     `R_pose = R_rest_transpose * R_njm`, and extract a new
     Rz·Ry·Rx-order euler triple from `R_pose` (inverse of the matrix-build
     step: `ry = asin(-R_pose[2][0])`, and, when `cos(ry)` is not ~0,
     `rx = atan2(R_pose[2][1]/cos(ry), R_pose[2][2]/cos(ry))`,
     `rz = atan2(R_pose[1][0]/cos(ry), R_pose[0][0]/cos(ry))`; in the
     gimbal-lock case (`cos(ry) ~ 0`), fall back to `rx = 0`,
     `rz = atan2(-R_pose[0][1], R_pose[1][1])`).
   - **Quaternion** and **scale** channels are written through directly with
     no rest-pose delta transform applied (quaternions are absolute pose
     values as-is; scale keyframes are similarly used directly).
3. Every keyframe's frame number is offset by `+1` when written to the
   target timeline (matching a 1-based frame convention), and each channel's
   interpolation mode is set uniformly according to the header's `interp`
   value (Linear or Bezier/Spline) for every keyframe point on every curve.

---

## 7. .rel Stage Files (All Platforms)

`n.rel` (and, for DC only, its paired `d.rel` sibling) is the on-disk stage/
map geometry format. All three platforms share the broad concept — a table of
"sections" (world-space-positioned sub-scenes), each containing a list of
static and/or animated mesh node references — but the concrete byte layouts
differ substantially per platform.

### 7.1 BB `n.rel` (PC stage format) — `NinjaStageGeometry`

**Footer-first parsing:** the file's **last 16 bytes** hold a single `u32`
"table offset" as their **first** field (the remaining 12 bytes of the footer
are not used by this parser).

```
seek to (file_size - 16)
u32 table_ofs
seek to table_ofs
u32 fmt2            (skipped/unused)
u32 n_count         (unused directly — see d_count below)
u32 d_count         # number of section descriptors
u32 hd              (skipped/unused)
u32 d_ofs           # offset to the section-descriptor table
u32 n_ofs           (unused by this parser)
```

**Section descriptor** (read `d_count` times, starting at `d_ofs`):
```
i32  section_id
f32  pos.x, f32 pos.y, f32 pos.z
i32  rot.x, i32 rot.y, i32 rot.z     (BAMS → radians, * 2π/65536)
f32  radius
u32  static_ofs
u32  animated_ofs
u32  static_num
u32  animated_num
u32  end            (unused)
```
For each section, build a section-local transform: `mat = rotate(rot);
mat.translate(pos)` (rotate-then-translate, per the standard convention in
§1.4). Then seek to `static_ofs` and, for `static_num` entries, read a `u32`
mesh offset followed by 12 skipped bytes (`0x0C` unused padding/attribute
bytes) per entry, collecting the mesh offset list. For each collected offset,
seek there and recursively parse a node tree (`readNode`, below) using the
section's transform as the initial parent matrix. (Note: this BB parser
reads only the `static_ofs` list; it does not currently traverse
`animated_ofs` entries for BB stages.)

#### 7.1.1 Node (`readNode`) — 52-byte layout, identical field order to NJ

```
u32 flags
u32 meshOfs
f32 pos.x, f32 pos.y, f32 pos.z
i32 rot.x, i32 rot.y, i32 rot.z    (BAMS)
f32 scl.x, f32 scl.y, f32 scl.z
u32 childOfs
u32 siblingOfs
```
Transform build: `rotate()` unless `flags & 0x02`, `translate()` unless
`flags & 0x01`, then `compose(parent)` if a parent matrix was supplied.
Bounds check (`>=` size on any of meshOfs/childOfs/siblingOfs) aborts this
node's subtree, matching NJ's convention. Traversal: mesh → child (new
parent = this node's matrix) → sibling (parent = the ORIGINAL parent passed
into this call, not this node's own matrix).

#### 7.1.2 Mesh (`readMesh`)

```
u32 flags
u32 vertex_info_list_offset
u32 vertex_info_count
u32 triangle_strip_list_a_offset
u32 triangle_strip_a_count
u32 triangle_strip_list_b_offset
u32 triangle_strip_b_count
f32 center.x, f32 center.y, f32 center.z
f32 radius
```
This is **byte-identical in layout to XJ's mesh header** (§3.3) — BB stage
meshes use the same vertex-info-array + dual-strip-list scheme as BB actor
models.

#### 7.1.3 Vertex info entries (16 bytes each) — same multi-entry UV-merge quirk as XJ

```
offset  size  field
0       2     vtype   (bit0=UV, bit1=normal, bit2=color — same bits as XJ)
2       2     unk
4       4     vofs
8       4     (vertex_size, unused)
12      4     vcount
```
**Identical quirk to §3.3.1**: loop over ALL `vertex_info_count` entries;
when re-visiting a previously-populated vertex index, **carry forward** any
previously-set `uv` value rather than clearing it if the current entry's
`vtype` lacks the UV bit. This is the exact bug described in the project's
"Vertex info multi-entry UV fix" note: only reading the first entry silently
drops UV data whenever a mesh has more than one vertex-info entry.

**Per-vertex layout** (read `vcount` times starting at `vofs`):
```
pos.x, pos.y, pos.z : 3x f32     (transformed by current section/node matrix)
if vtype & 0x02: norm.x, norm.y, norm.z : 3x f32   (transformed as a normal)
if vtype & 0x04: b, g, r, a : 4x u8, each /255.0   (BGRA order — same as NJ, opposite of XJ's RGBA)
if vtype & 0x01: u, v : 2x f32; stored uv = (u, 1.0 - v)
```

#### 7.1.4 Strip lists and materials

Identical strip-descriptor layout and winding rule to XJ's strip list —
`(material_property_list_offset, material_property_list_size,
index_list_offset, index_count, unknown)` — **except** BB stage strip winding
alternates per NJ's convention (starting `clockwise = False`, flipping every
triangle, single winding per triangle emitted — **not** the XJ double-winding
scheme):
```
clockwise = False
for i in range(len(points) - 2):
    if clockwise: (a,b,c) = (points[i], points[i+2], points[i+1])
    else:          (a,b,c) = (points[i+1], points[i+2], points[i])
    clockwise = not clockwise
    if a!=b and b!=c and c!=a: emit triangle (a,b,c)
```
Material properties for BB stages **do** support a double-sided flag
(`mat_type == 4`, 12 bytes payload skipped, sets `doubleSided = True`) — this
is the same set of material property types as NJ's chunk materials (§2.4.5)
reused in a flat (non-chunked) property-list format:
```
mat_type == 2: u32 dst, u32 src (blend mode, same src/dst==1/4 or 5/4 logic as NJ), then 4 bytes skipped
mat_type == 3: u32 tex_id → material.texIndex, then 8 bytes skipped
mat_type == 4: 12 bytes skipped; sets doubleSided = True
mat_type == 5: u8 r,g,b,a (each /255.0, RGBA order) → diffuse, then 8 bytes skipped
else: 12 bytes skipped
```

### 7.2 DC `n.rel` / `d.rel` pairing — `NinjaDCRelImporter`

**Overview:** DC stages are split across a pair of sibling files sharing the
same base stem but differing in their trailing type letter: `...n.rel`
("normal"/static-heavy geometry) and `...d.rel` ("dynamic"/dark? — exact
semantic unclear but the pairing convention is what matters), both located in
the same directory. **Both are optional individually** — the importer proceeds
if at least one of the pair exists — but section data referencing meshes in
the missing file simply cannot be resolved.

**QUIRK (case sensitivity — must always be handled):** filename matching for
locating the sibling file, for detecting whether a `.rel` file is DC-format at
all (`detect_platform`), and for the DC stage import execute path, **must
always compare/derive names using `.lower()`**. Uppercase-named files (e.g.
`MAP_FOREST01N.REL`) will silently fail platform detection or fail to locate
their sibling `d.rel`/`D.REL` file unless every comparison point
lower-cases both the base stem and the trailing type-letter before comparing.
The reference implementation preserves the *original* case when constructing
the sibling path to open (matching whatever case convention the discovered
directory entry uses — see `IMPORT_OT_pso_stage.execute`, which derives
`d_path`/`n_path` by checking `stem[-1].isupper()` and substituting `'D'`/`'N'`
vs `'d'`/`'n'` accordingly) but performs the *lookup/matching* itself
case-insensitively.

#### 7.2.1 Per-file preparation (`_prepare`, run once for each of `d.rel` and `n.rel` that exists)

**Footer:** same convention as BB — last 16 bytes hold `u32 table_ofs` as
their first field.
```
seek (file_size - 16); u32 table_ofs
seek table_ofs
u32 section_count
u32 magic         (skipped/unused)
u32 section_ofs
u32 texture_ofs
```

**Texture name table** (only relevant/parsed once — texture indices from
whichever file is processed first populate `self.texNames`; texNames already
populated by an earlier file are not re-read from the second file, since the
list only grows by index and existing indices are preserved):
```
seek texture_ofs
u32 tn_ofs
u32 tn_count
seek tn_ofs
for tn_count entries:
    u32 name_ofs
    save_ofs = current_position + 8   # skip 8 bytes of unused per-entry fields BEFORE reading the next entry's name_ofs
    seek name_ofs; read null-terminated string; seek back to save_ofs
```

**Section table** (`section_count` entries, at `section_ofs`):
```
i32 section_id
f32 pos.x, f32 pos.y, f32 pos.z
i32 rot.x, i32 rot.y, i32 rot.z    (BAMS)
f32 radius            (skipped/unused beyond being read)
u32 a_ofs, u32 b_ofs, u32 c_ofs
u32 a_num, u32 b_num, u32 c_num
u32 end                (skipped/unused)
```
For each section, a shared-by-id dict entry `sections[str(section_id)]` is
created **only if this section id hasn't already been seen** (so that a
section id shared between `d.rel` and `n.rel` accumulates entries from both
files into one combined section rather than overwriting). Its `pos`/`rot` are
recorded from whichever file's section table is processed first for that id.
Then:
```
seek a_ofs; for a_num entries: u32 m_ofs, then skip 0x2C (44) bytes → append {'src': <'d' or 'n'>, 'm_ofs': m_ofs} to sections[id]['static']
seek c_ofs; for c_num entries: u32 m_ofs, u32 (skipped), then skip 0x34 (52) bytes → append {'src': ..., 'm_ofs': m_ofs} to sections[id]['animated']
```
(Note: field `b_ofs`/`b_num` from the section header are read but never used
by this parser — only the `a_` ("static") and `c_` ("animated") lists are
consumed.)

#### 7.2.2 Assembling sections (`_readSections`)

For every accumulated section (across both files, keyed by section id),
build the section transform (`rotate(rot); translate(pos)`), then for every
entry in `section['static'] + section['animated']` (combined list, static
entries first): switch the active BitStream to whichever of `bs_d`/`bs_n`
matches that entry's recorded `src` label, seek to `m_ofs`, **reset the
vertex stack to empty** (each mesh-node-tree traversal starts fresh — vertex
indices are NOT shared across different section-entry traversals, unlike
within a single NJ chunk-stream mesh), and recursively read a node tree
(`_readNode`) using the section transform as the initial parent matrix.

#### 7.2.3 Node and mesh layout

Byte-identical 52-byte node layout to §7.1.1 (same field order: flags,
meshOfs, pos, rot(BAMS), scl, childOfs, siblingOfs), and mesh header:
```
u32 vofs
u32 cofs
[16 bytes: center(3f)+radius(1f), skipped]
```
— this is the **NJ-style two-stream mesh** (vertex-defining chunk stream at
`vofs`, polygon-consuming chunk stream at `cofs`), NOT the XJ/BB-stage
vertex-info-array style. Both `readChunks` calls (§2.4) run against whichever
BitStream (`bs_d` or `bs_n`) is currently active, using the full
`NinjaChunkMixin` chunk-type dispatch machinery from Section 2 (vertex chunks,
material chunks, strip/volume chunks, bits/branch chunks, all identical
semantics to native `.nj` actor files). DC `.rel` files are always
little-endian (no big-endian auto-detection is performed for `.rel`, unlike
`.nj` actor files).

### 7.3 GC `n.rel` (standard Flipper stage format) — `FlipperGCImporter.parse_stage`

**Always big-endian.** Footer convention matches the others:
```
seek (file_size - 16); u32 table_ofs (BE)
seek table_ofs
u32 fmt2           (skipped)
u32 n_count        (skipped/unused)
u16 d_count        # number of section descriptors — NOTE: 16-bit here, unlike BB/DC's 32-bit d_count
u16 padding        (skipped)
u32 hd             (skipped)
u32 d_ofs
u32 tex_ofs
```

**Texture name table** (at `tex_ofs`):
```
u32 tn_ofs
u32 tn_count
seek tn_ofs
for tn_count entries:
    u32 name_ofs
    save_ofs = current_position + 8
    seek name_ofs; read null-terminated string; seek save_ofs
```
Each texture name's extension-stripped basename (or the full name if it has
no extension) is assigned directly to `self.textures[i]['name']` by index —
**no** GJTL-style case-insensitive remapping is performed here (unlike §4.2's
`_readTexList`); the texture archive is assumed to already be in the same
order as this name table for stage files.

**Section table** (at `d_ofs`, `d_count` entries — note the parser explicitly
tracks and restores its own read cursor per iteration via a `save_pos`
variable, because the two per-section sub-lists it reads below are located
elsewhere in the file and it must return to the right spot to read the next
section header):
```
per section:
    i32 section_id       (skipped/unused beyond being read)
    f32 pos.x, pos.y, pos.z
    i32 rot.x, rot.y, rot.z    (BAMS)
    f32 radius            (skipped)
    u32 ptr_a             # offset to static-mesh reference list
    u32 ptr_b             # offset to animated-mesh reference list
    u32 cnt_a
    u32 cnt_b
    u32 end               (skipped)
```
Build `sec_mat = rotate(rot); translate(pos)`.

**List A (static mesh references)**, `cnt_a` entries at `ptr_a`:
```
u32 m_ofs            # node offset to traverse
u32 attr1            (skipped)
u32 attr2            (skipped)
u32 f                # flags — used to decide whether to skip this entry, see below
```

**List B (animated mesh references)**, `cnt_b` entries at `ptr_b`:
```
u32 m_ofs
u32 a_ofs             (skipped)
[8 bytes skipped]
f32 speed             (skipped)
[8 bytes skipped]
u32 f                # flags
```

**Skip-flag filtering:** certain exact flag values, plus a shared "hide"
bitmask, cause a mesh reference to be skipped entirely (not traversed):
```
SKIP_A = {0x010225, 0x010204, 0x010205, 0x010264}    # exact-match skip set for list-A entries
SKIP_B = {0x010244, 0x010204}                        # exact-match skip set for list-B entries
# additionally, for BOTH lists: skip if (f & 0x200) != 0
```
For every non-skipped entry (from either list), reset the "stop" flag, seek
to `m_ofs`, and traverse the node tree (`_readNode`, §4.3) using `sec_mat` as
the section-level parent matrix (via a wrapper dict `{'matrix': sec_mat,
'idx': -1}`, matching the `{matrix, idx}` "pNode" convention `_readNode`
expects).

### 7.4 GC Episode III Card Revolution `n.rel` variant

**Overview:** Episode III ("Card Revolution") stage files are a **distinct,
much smaller descriptor format** layered on top of the same underlying
GJCM/GJTL-based geometry primitives, rather than the full section-table
format in §7.3. Detection and structural specifics for this variant, as
findable in the codebase:

- **Detection is footer-based**, exactly mirroring the mechanism
  `detect_platform` uses (§9): read the file's last 16 bytes as big-endian,
  take the first `u32` as `table_ofs`, and check whether the 4 bytes at
  `data[table_ofs : table_ofs+4]` spell the ASCII marker `"fmt2"` (bytes
  `66 6D 74 32`). If so, the file is treated as GC-format regardless of
  whether `GJCM`/`GJTL` byte sequences are found anywhere else in the file —
  this exact `"fmt2"` footer-pointed marker is what distinguishes the Card
  Revolution `.rel` layout (which otherwise wouldn't be identifiable as GC via
  a `GJCM`/`GJTL` magic byte scan, since it may not embed raw GJCM chunks the
  same way full stage `.rel`s do).
- The task description also specifies a **608-byte scene descriptor starting
  with the string `"map_sky\0"`** as part of this variant's on-disk layout.
  This literal string and fixed 608-byte descriptor size were **not found
  verbatim anywhere in the current codebase** (`__init__.py`) — the importer's
  `parse_stage` method (§7.3) is used unconditionally for every file that
  `detect_platform` classifies as `'GC'`, including ones detected purely via
  the `"fmt2"` footer marker (there is no separate/special-cased Card
  Revolution parsing path implemented). If your own implementation needs to
  support the "map_sky\0"-prefixed 608-byte descriptor structure specifically,
  that layout must be sourced from the actual game files or another reference
  (e.g. by dumping and diffing an Episode III stage file against a standard
  Episode I/II stage file) — it is not present in this importer's source and
  cannot be documented byte-accurately from it. What IS confirmed from this
  codebase is the exact detection trigger (the `"fmt2"` footer-pointed ASCII
  marker) that routes a file into the GC parsing path in the first place.

---

## 8. Texture Archive Formats (XVM / PVM / GVM)

`load_texture_archive(data)` auto-detects which of the three archive formats a
byte buffer holds by inspecting its first 4 bytes:
```
if first 4 bytes in {"XVMH", "XVRT"}: XVM loader
elif first 4 bytes in {"GVMH", "GVRT"}: GVM loader
else: PVM loader (default fallback — PVM archives have no reliably-distinct
                  leading magic check performed at this dispatch level;
                  see §8.2's own internal PVMH/PVRT scan)
```
All three loaders return a list of `{'name': str, 'width': int, 'height': int,
'pixels': bytes}` dicts, where `pixels` is always decoded to top-to-bottom,
row-major, 4-bytes-per-pixel **RGBA8** regardless of source format.

### 8.1 XVM (PC/Blue Burst texture archive)

```
u32 magic          # must equal "XVMH" (0x484D5658) — LE
u32 archive_length (skipped/unused — texture entries are found by scanning, not by trusting this length)
u32 texture_count  (skipped/unused for the same reason)
```
Then scan forward through the rest of the buffer for `"XVRT"` (`0x54525658`)
magic words; for each found:
```
u32 magic == XVRT
u32 chunk_length   (skipped — texture header fields immediately follow)
```
record the position immediately after this length field as a texture-entry
start offset. Once all XVRT offsets are collected, for each:
```
u32 format_1        # color format, NOT used by the decoder (compression alone determines decode path)
u32 format_2        # compression/"fmt2" type: 6=DXT1, 7=DXT3, 8=DXT5 (anything else falls back to DXT1 decode)
u32 tex_id          # index within archive (not otherwise used by the loader — sequential position is used for naming instead)
u16 width
u16 height
u32 size            # byte length of the following compressed pixel payload
[0x24 (36) bytes of padding/header tail, skipped]
<size> bytes         # raw block-compressed pixel data
```
Decode dispatch: `format_2 == 7` → DXT3; `format_2 == 8` → DXT5; anything else
(including 6) → DXT1. Textures are named sequentially `"Texture_0"`,
`"Texture_1"`, etc., at load time (real names are assigned later from the
model's NJTL/NJTL-equivalent chunk, by index, when available).

#### 8.1.1 DXT1 (BC1) decode algorithm

Each 8-byte block covers a 4x4 pixel area. Block layout: `u16 color0, u16
color1` (both RGB565-packed), then `u32 index_bits` (2 bits per pixel, 16
pixels, LSB-first — pixel `(py,px)`'s 2-bit code is at bit position
`2*(py*4+px)`).
```
c0 = unpack_rgb565(color0); c1 = unpack_rgb565(color1)
if color0_raw > color1_raw:   # 4-color mode
    palette = [c0(a=255), c1(a=255),
               lerp_2_3(c0,c1)(a=255),   # (2*c0+c1)/3
               lerp_1_3(c0,c1)(a=255)]   # (c0+2*c1)/3
else:                          # 3-color + transparent mode
    palette = [c0(a=255), c1(a=255),
               avg(c0,c1)(a=255),        # (c0+c1)/2
               (0,0,0,0)]                # fully transparent black
for each of the 16 pixels: look up its 2-bit code in `palette`, write RGBA
```
RGB565 unpack: `r = (v>>11 & 0x1F) * 255 // 31`, `g = (v>>5 & 0x3F) * 255 //
63`, `b = (v & 0x1F) * 255 // 31`.

#### 8.1.2 DXT3 (BC2) decode algorithm

Each 16-byte block: first 8 bytes are an **explicit 4-bit alpha map** (no
interpolation), last 8 bytes are a **DXT1 color block forced into 4-color
mode** (color decode ignores the color0>color1 comparison and always uses the
4-color palette, and does NOT touch the alpha channel — alpha comes purely
from the explicit map).
```
alpha block: 8 bytes = 4 rows x 2 bytes; each row is a u16 with 4 nibbles
             (4 bits per pixel, low-to-high = left-to-right within the row);
             final alpha = nibble * 17   (0xF*17=255, 0*17=0)
color block: decoded exactly like DXT1's 4-color-mode palette (see §8.1.1),
             but its alpha output is discarded/ignored — the explicit alpha
             block above is authoritative
```

#### 8.1.3 DXT5 (BC3) decode algorithm

Each 16-byte block: 2 alpha reference bytes + 6 bytes of packed 3-bit alpha
indices (48 bits = 16 pixels x 3 bits, packed little-endian across the 6-byte
span), followed by an 8-byte DXT1-style color block (again forced to
4-color mode, alpha discarded from the color block).
```
a0, a1 = the two reference alpha bytes
if a0 > a1:  # 8-value interpolated ramp
    apal = [a0, a1,
            (6a0+1a1)/7, (5a0+2a1)/7, (4a0+3a1)/7,
            (3a0+4a1)/7, (2a0+5a1)/7, (1a0+6a1)/7]
else:        # 6-value ramp + explicit 0 and 255
    apal = [a0, a1,
            (4a0+1a1)/5, (3a0+2a1)/5, (2a0+3a1)/5, (1a0+4a1)/5,
            0, 255]
for each of 16 pixels: 3-bit index into apal → alpha value
color: same DXT1 4-color-mode decode as DXT3, alpha discarded from it
```

### 8.2 PVM (Dreamcast PowerVR texture archive)

**Container scan:** scan the buffer byte-by-byte for either a `"PVMH"` tag
(multi-texture archive) or a bare `"PVRT"` tag (single-texture file with no
outer header at all) — whichever is found first, at the lowest offset, wins.

**Bare single-PVRT case:**
```
"PVRT" (4 bytes), u32 (skipped — chunk length not trusted for seeking here)
u8 color_fmt, u8 data_fmt
u16 width, u16 height   (LE)
<remaining bytes> : raw PVR pixel payload, decoded via decode_pvrt (§8.2.2)
```
Single texture is named `"texture_000"`.

**PVMH multi-texture header:**
```
"PVMH" (4 bytes)
u32 pvmh_len       # a nominal chunk length — see the "quirk" note below regarding why it's not trusted for seeking
u16 flags
u16 tex_count
```
For each of `tex_count` entries, **fields are conditionally present based on
`flags` bits** (this is the same conditional-header-field pattern as GVM,
§8.3):
```
u16 index                              # always present
if flags & 0x08:  28 bytes (0x1C) ASCII name, right-trimmed of trailing NULs → texture name
if flags & 0x04:  2 bytes skipped (an unused/format field)
if flags & 0x02:  u16 size_value, decoded as:
                      width  = 1 << ((size_value & 0x0F) + 2)
                      height = 1 << (((size_value >> 4) & 0x0F) + 2)
if flags & 0x01:  u32 → overwrites this entry's `index` field (a "GUID"/absolute-index override)
```

**QUIRK — do not trust `pvmh_len` for locating the first PVRT chunk.** Some DC
`.pvm` files count `pvmh_len` from the very start of the `PVMH` block
(including its own 8-byte magic+length prefix), while others count it from
just after that prefix — this inconsistency means computing `save_position +
pvmh_len` can overshoot the true first `PVRT` tag location by up to 8 bytes.
The robust approach used here: after finishing the conditional entry-table
parse above, use **whichever is larger** of (a) the current read cursor
position (`pos`, i.e. wherever entry parsing naturally left off) and (b)
`save_position + pvmh_len` (the nominal declared end) as the starting point to
scan forward for the literal `"PVRT"` byte sequence.

For each of the `tex_count` entries (in order), scan forward from the current
position for the next `"PVRT"` tag; once found:
```
"PVRT" (4 bytes, already matched by the scan)
u32 chunk_payload_length     # LE — the length of just this texture's PVR payload region
<payload>:
    u8 color_fmt, u8 data_fmt
    u16 width, u16 height    (LE)
    <chunk_payload_length - 8 remaining bytes> : PVR pixel data, decoded via decode_pvrt
```
The texture's final name is the entry's name with its extension stripped (if
it had a `.` in it), or a generic `"texture_NNN"` fallback if the name ended
up empty (e.g. no name flag was set).

#### 8.2.1 PVR color formats (`color_fmt` byte)

| value | name | decode |
|---|---|---|
| 0 | ARGB_1555 | `a = 255 if bit15 else 0`; `r=(v>>10&0x1F)*255/31`; `g=(v>>5&0x1F)*255/31`; `b=(v&0x1F)*255/31` |
| 1 | RGB_565 | `r=(v>>11&0x1F)*255/31`; `g=(v>>5&0x3F)*255/63`; `b=(v&0x1F)*255/31`; `a=255` |
| 2 | ARGB_4444 | `a=(v>>12&0xF)*255/15`; `r=(v>>8&0xF)*255/15`; `g=(v>>4&0xF)*255/15`; `b=(v&0xF)*255/15`; **special case: if r/g/b nonzero but a==0, force a=255** (avoids fully-invisible-but-colored pixels from lossy round-tripped alpha-4444 sources) |
| other | unknown | gray placeholder `(128,128,128,255)` |

#### 8.2.2 PVR pixel layout modes (`data_fmt` byte) and decode algorithm

```
TWIDDLED  = {0x01, 0x02, 0x0D, 0x12}
VQ        = {0x03, 0x04, 0x10, 0x11}
RECTANGLE = {0x09}
HAS_MIPS  = {0x02, 0x04, 0x06, 0x08, 0x0F, 0x11, 0x12}
```

**Mipmap skip (non-VQ mip formats):** if `data_fmt in HAS_MIPS and data_fmt
not in VQ`, the payload stores mip levels **smallest-first**; skip past all
smaller mips to reach the full-size level before decoding. Mipmap byte-count
algorithm (`_pvr_mipmap_skip`, `compressed=False` for this non-VQ path):
```
compute mip = number of times width can be right-shifted before reaching 0
              (i.e. mip = floor(log2(width)) + 1, roughly — the exact loop:
              mip counts up while shifting a working copy of width right
              until it becomes 0)
skip = 0
while mip > 0:
    mw = width  >> (mip - 1)
    mh = height >> (mip - 1)
    mip -= 1
    if mip > 0:   # this is a "smaller" mip level being skipped
        skip += (mw*mh // 4) if compressed else (mw*mh*2)
    else:          # this is the smallest (1x1) mip — special-cased minimal size
        skip += 1 if compressed else 2
```
(`compressed=True` variant of this same function, used for VQ-format mip
index arrays, uses `mw*mh//4` per level and `1` for the smallest level instead
of `mw*mh*2`/`2` — i.e. VQ index data is 4x smaller per level than raw pixel
data, since VQ indices are 1 byte per 2x2 pixel block.)

**VQ (Vector Quantization) decode — `data_fmt in VQ`:**

1. Determine codebook size: default `256` entries; if `data_fmt in {0x10,
   0x11}` (the "SMALLVQ" variants), the codebook size instead depends on
   `width`: `<=16 → 16`, `==32 → 32`, `==64 → 128` (any other width keeps the
   default 256).
2. **QUIRK — the codebook is ALWAYS located at payload offset 0, and
   `mipmap_skip` must run AFTER reading the codebook, never before.** This
   was a previously-fixed real bug: applying the mipmap-skip byte count
   *before* reading the codebook garbled nearly every VQ+Mip-format DC stage
   texture, because doing so skipped into the middle of the codebook data
   itself rather than past it. The correct order is:
   ```
   pos = 0                          # start of payload — do NOT pre-skip
   read `cb_size` codebook entries, each entry = 4 sub-colors (a 2x2 pixel
       quad), each sub-color a u16 read via the color_fmt decoder (§8.2.1)
       → this consumes cb_size * 4 * 2 bytes starting at offset 0
   if data_fmt in HAS_MIPS:
       pos += _pvr_mipmap_skip(width, height, compressed=True)
       # ONLY NOW, after the codebook, do we skip past the smaller VQ
       # mip-index arrays to reach the full-size index data.
   idx_start = pos
   ```
3. **Untwiddled index lookup**: the full-size index array is stored in
   twiddled (Morton/Z-order) order across a `(width/2) x (height/2)` grid of
   2x2 quads (see §8.2.3 for the untwiddle bit-interleave function). For each
   quad coordinate `(x, y)` in `range(width//2) x range(height//2)`:
   ```
   i = untwiddle(x, y)
   quad = codebook[raw_index_byte_at(idx_start + i) % codebook_size]
   write the 4 sub-colors of `quad` to output pixels
       (2x, 2y), (2x+1, 2y), (2x, 2y+1), (2x+1, 2y+1)
       in that exact order (quad index 0=top-left, 1=top-right,
       2=bottom-left, 3=bottom-right, i.e. row-major within the 2x2 block)
   ```

**Twiddled (Morton-order) decode — `data_fmt in TWIDDLED`:** every pixel
`(x, y)` in the full `width x height` grid maps to a twiddled byte offset
`i = untwiddle(x, y)`; read a `u16` color value at `payload_offset + i*2`
(after any mip-skip prefix already applied per the non-VQ mip rule above),
decode via `color_fmt`.

**Rectangle (linear/raster) decode — `data_fmt in RECTANGLE (0x09)`:** simplest
case — pixels are stored row-major, left-to-right, top-to-bottom, each as one
`u16` color value decoded via `color_fmt`, with no twiddling and no mip
handling at all.

Any other `data_fmt` value produces an all-zero (fully transparent black)
output buffer (the decoder silently falls through with no matched branch).

#### 8.2.3 Untwiddle (bit-interleave) algorithm

PowerVR "twiddled" texture layout interleaves the bits of X and Y coordinates
(Morton/Z-order curve), used both for whole-texture twiddled pixel data and
for VQ index arrays:
```
def untwiddle_1d(v):
    r = 0
    for i in range(10):           # supports up to 10-bit coordinates (1024)
        bit = 1 << i
        if v & bit:
            r |= bit << i          # spread each input bit to every OTHER output bit position
    return r

def untwiddle(x, y):
    return untwiddle_1d(y) | (untwiddle_1d(x) << 1)
    # y's bits occupy even output-bit positions, x's bits occupy odd
    # output-bit positions, interleaved — the classic Morton-code pattern
```

### 8.3 GVM (GameCube texture archive)

Container structure mirrors PVM closely but with **big-endian multi-byte
fields** wherever indicated below, and GameCube-native GVR pixel-format
decoding (§8.3.2) instead of PVR.

**Container scan:** scan for `"GVMH"` (multi-texture archive) or a bare
`"GVRT"` (single-texture file), matching whichever appears first.

**Bare single-GVRT case:**
```
"GVRT" (4 bytes)
u32 chunk_len         (LE — NOTE: this length field itself is little-endian
                       even though the header fields that follow are
                       big-endian; only the payload's own internal fields
                       and the GVMH multi-entry table below use BE)
u8 (1 byte skipped — reserved), u8 pixel_fmt_byte
    → pixel_fmt = pixel_fmt_byte >> 4   (upper nibble only)
u8 data_fmt
u16 width, u16 height     (BE)
<remaining bytes up to chunk_len> : raw GVR pixel data, decoded via decode_gvr (§8.3.2)
```
Single texture named `"texture_000"`.

**GVMH multi-texture header:**
```
"GVMH" (4 bytes)
u32 chunk_len          (LE — same LE/BE split as above: this outer length is LE)
gvmh_end = current_position + chunk_len
u16 flags, u16 tex_count      (BE)
```
For each of `tex_count` entries (fields conditionally present per `flags`,
identical bit meanings to PVM's flags — see §8.2):
```
u16 index                                    (BE, always present)
if flags & 0x08:  28 bytes (0x1C) ASCII name, right-trimmed of NULs
if flags & 0x04:  2 bytes skipped (pixel-format field, unused for decoding — actual format comes from each GVRT chunk's own header)
if flags & 0x02:  u16 size_value (BE), decoded exactly like PVM:
                      width  = 1 << ((size_value & 0x0F) + 2)
                      height = 1 << (((size_value >> 4) & 0x0F) + 2)
if flags & 0x01:  4 bytes skipped (GUID/index field — NOT applied to override `index` here, unlike PVM's equivalent flag bit which does overwrite the entry's index)
```

**Scanning for GVRT chunks:** start the scan at `max(current_position,
gvmh_end)` (same "trust whichever is farther along" defensive pattern as
PVM's PVMH-length quirk, §8.2). For each of the `tex_count` entries, scan
forward for the literal `"GVRT"` tag; once found:
```
"GVRT" (4 bytes)
u32 chunk_len (LE)
pstart = current_position
<chunk_len bytes follow — payload region>
    at pstart:
        u8 (1 byte skipped), u8 pixel_fmt_byte → pixel_fmt = pixel_fmt_byte >> 4
        u8 data_fmt
        u16 width, u16 height   (BE)
        <chunk_len - 8 bytes> : raw GVR pixel payload, decoded via decode_gvr
```
Texture name: entry's name with extension stripped if present, else
`"texture_NNN"` fallback (same convention as PVM).

#### 8.3.1 GVR pixel color sub-formats used internally by `decode_gvr`

- **RGB565** (big-endian `u16`): `r=(v>>11&0x1F)*255/31`, `g=(v>>5&0x3F)*255/63`, `b=(v&0x1F)*255/31`, `a=255`.
- **RGB5A3** (big-endian `u16`): if bit 15 set → opaque RGB555
  (`r=(v>>10&0x1F)*255/31`, `g=(v>>5&0x1F)*255/31`, `b=(v&0x1F)*255/31`,
  `a=255`); else → translucent RGB4A3 (`a=(v>>12&0x7)*255/7`,
  `r=(v>>8&0xF)*255/15`, `g=(v>>4&0xF)*255/15`, `b=(v&0xF)*255/15`).

#### 8.3.2 GVR data formats (`data_fmt` byte) — tile layouts and decode algorithms

All GVR formats use fixed-size hardware texture tiles; pixels are stored tile
by tile, left-to-right/top-to-bottom in *tile* order, and left-to-right/
top-to-bottom *within* each tile.

| data_fmt | name | tile size | bpp | notes |
|---|---|---|---|---|
| 0x00 | I4 | 8x8 | 4 | luminance-only; 2 pixels/byte, `i0 = high_nibble*17`, `i1 = low_nibble*17`; output RGBA = (i,i,i,255) |
| 0x01 | I8 | 8x4 | 8 | luminance-only; 1 byte/pixel; RGBA = (i,i,i,255) |
| 0x02 | IA4 | 8x4 | 8 | 1 byte/pixel, high nibble = intensity, low nibble = alpha; each `*17`; RGBA=(i,i,i,a) |
| 0x03 | IA8 | 4x4 | 16 | 1 BE u16/pixel: high byte = alpha, low byte = intensity; RGBA=(i,i,i,a) |
| 0x04 | RGB565 | 4x4 | 16 | 1 BE u16/pixel, decoded per §8.3.1 |
| 0x05 | RGB5A3 | 4x4 | 16 | 1 BE u16/pixel, decoded per §8.3.1 |
| 0x06 | RGBA8 | 4x4 | 32 | see below — split AR/GB sub-blocks |
| 0x0E | CMPR | 8x8 super-tile of 2x2 DXT1 sub-blocks | 4 (avg) | see below |
| other | (unsupported) | — | — | entire output filled with solid magenta (255,0,255,255) as an "obviously broken" placeholder |

**RGBA8 (0x06) sub-block layout:** each 4x4 tile is stored as **two 32-byte
sub-blocks concatenated** (64 bytes total per tile): the first 32 bytes are an
"AR" sub-block (alpha+red interleaved, 2 bytes per pixel position, 16 pixel
positions), the second 32 bytes are a "GB" sub-block (green+blue interleaved,
same layout). For pixel index `k = ty*4+tx` within the tile: `a =
ar_block[2k]`, `r = ar_block[2k+1]`, `g = gb_block[2k]`, `b = gb_block[2k+1]`.

**CMPR (0x0E) super-tile layout:** an 8x8 pixel region ("super-tile") holds
**four** 4x4 DXT1-style sub-blocks arranged 2x2 (top-left, top-right,
bottom-left, bottom-right), each sub-block being the standard 8-byte DXT1
block format (`u16 color0, u16 color1, u32 index_bits` — but **color
endpoints are big-endian** here, unlike PC DXT1's little-endian encoding).
Palette construction (4-color if `color0_raw > color1_raw`, else 3-color +
transparent) is identical logic to standard DXT1 (§8.1.1). **Index bit order
differs from PC DXT1**: the 4-byte index table is 4 bytes (one per tile row),
and within each row byte, the 2-bit codes are packed **MSB-first**: pixel 0's
code occupies bits `[7:6]`, pixel 1's occupies `[5:4]`, pixel 2's occupies
`[3:2]`, pixel 3's occupies `[1:0]` — i.e. `code = (row_byte >> (6 - 2*tx)) &
3` for column `tx` in `0..3`. This is the opposite of PC DXT1's LSB-first,
32-bit-packed index convention (§8.1.1) and must be handled as a genuinely
distinct bit-unpacking routine, not merely a byte-order swap of the same
scheme.

---

## 9. Platform / File Autodetection Logic

`detect_platform(filepath)` determines whether a given model/stage file
belongs to `'BB'`, `'DC'`, or `'GC'`:

```
ext = lowercase file extension

# 1. Actor-model files: extension alone is unambiguous
if ext == '.xj': return 'BB'
if ext == '.gj': return 'GC'
if ext == '.nj': return 'DC'

# 2. Stage files (.rel and anything else falls through to here)

# 2a. DC sibling-pair detection (case-insensitive)
name_stem = filename without extension
if name_stem is non-empty and its last character, lowercased, is 'n' or 'd':
    base = name_stem[:-1], lowercased
    other_letter = 'd' if last char (lowercased) was 'n' else 'n'
    scan the file's directory (case-insensitively: compare
        entry_stem.lower() == base + other_letter, and
        entry_ext.lower() == '.rel')
    if a matching sibling file is found: return 'DC'

# 2b. GC magic-byte scan
open and read the whole file
if literal bytes b'GJCM' or b'GJTL' appear ANYWHERE in the file: return 'GC'
    (this is a raw substring search over the whole buffer, not a
    structured chunk-table walk — any occurrence, even accidental,
    of those 4 bytes triggers GC classification)

# 2c. GC stage fmt2-footer detection (for Card Revolution / other .rel
#     variants that don't literally contain "GJCM"/"GJTL" bytes)
if file length >= 20:
    table_ofs = big-endian u32 read from the last 16 bytes (first field)
    if table_ofs + 4 <= file length and
       data[table_ofs : table_ofs+4] == b'fmt2':
        return 'GC'

# 3. Default fallback
return 'BB'
```

Key properties to note for a from-scratch reimplementation:
- Actor-file detection never touches file contents — extension alone decides.
- Stage-file (`.rel`) detection is **content-aware** and tries three
  increasingly expensive strategies in order: (a) cheap directory-listing
  sibling check for DC, (b) whole-file substring scan for GC magics, (c)
  footer-pointed 4-byte marker check for GC's alternate ("fmt2") stage
  layout. Only if **none** of these succeed does the function assume BB.
- The DC check happens *before* the GC checks, so a file that happens to have
  both a DC-style sibling *and* GC magic bytes present would be classified DC
  — sibling-pair detection takes priority. In practice this collision should
  not occur since DC and GC stage files are structurally exclusive.

---

## 10. Texture-Archive-to-Model Association Strategies

Because texture archives can be supplied in several different ways depending
on how a given PSO release packages its assets, this importer tries multiple
strategies, roughly ordered from "most specific / most likely correct" to
"least specific / last resort":

### 10.1 For single actor/stage file imports (`IMPORT_OT_pso_actor`, `IMPORT_OT_pso_stage`)

1. **User-specified path** (`xvm_filepath` property, if non-empty) — resolved
   relative to the model file's directory. Always wins if provided.
2. **Compound-extension sidecar** (`find_compound_tex_path`, §1.7) — e.g.
   `model.nj.xvm` beside `model.nj`. Tried next.
3. **Name-based / directory-fallback search** (`find_tex_archive`, §1.6) —
   stem-stripping plus platform-prioritized extension search, falling back to
   "only one archive of this type in the directory" heuristic.

If none of these locate an existing file, texture loading is skipped
entirely and a warning is reported; geometry import still proceeds (with no
textures assigned).

### 10.2 For BML archives (`IMPORT_OT_pso_bml`) — per-model, four-tier fallback

For **each** model entry extracted from the BML (§5), in this exact priority
order:

1. **Embedded-in-BML pairing**: if `bml_read` paired this model entry with an
   immediately-following texture entry (standard layout, §5.2) OR a
   compound-named entry elsewhere in the archive (`modelname.xvm`/`.gvm`/
   `.pvm`, matched case-insensitively against every other entry's filename),
   decode that embedded archive's bytes directly.
2. **Per-model sidecar file** beside the `.bml` on disk: for each of
   `.gvm`/`.GVM`/`.pvm`/`.PVM`/`.xvm`/`.XVM` (tried in that fixed order), check
   for a file named `<bml_directory>/<model_stem><ext>` (where `model_stem`
   is the BML-internal entry's filename minus its own extension — e.g.
   `robby_cat.GVM` beside the `.bml`, for an internal entry `robby_cat.nj`).
3. **BML-level shared sidecar**: a single texture archive named after the
   `.bml` file itself (`<bml_stem><ext>`, again trying
   `.gvm`/`.GVM`/`.pvm`/`.PVM`/`.xvm`/`.XVM` in order, loaded once up-front and
   reused for every model in the archive that reaches this fallback tier).
4. **LOD/variant texture reuse from the previous model in the same BML**: if
   none of the above yielded any textures for this model, and a *previous*
   model in the same BML *did* have textures resolved (by any of tiers 1–3),
   reuse that previous model's texture list verbatim. This specifically
   supports BML archives that bundle a main model plus LOD/variant
   sub-models (commonly prefixed `lo_`, `hi_`, `sd_` in PSO's asset naming
   convention) which carry no texture data of their own and are expected to
   share whatever texture set their preceding "main" sibling entry resolved
   to. Models are processed in the BML's on-disk archive order, so this only
   looks backward, never forward.

If a model still has no textures after all four tiers, it is imported with
no material texture assigned (solid diffuse color / no texture node).

### 10.3 GJTL-name-based remapping vs. absolute-index fallback (GC-specific)

Independent of which of the above tiers supplied the texture archive bytes, a
`.gj` model's own internal `_readTexList` (§4.2) performs an additional
**index remapping** step once both a `GJTL` name table and a loaded texture
archive are available: if the loaded archive has at least one texture with a
"real" (non-generic, i.e. not `texture_NNN`-pattern) name, textures are
reordered/matched by case-insensitive name lookup against the GJTL name list,
so that strip data's small GJTL-relative `tid` values (§4.7) correctly index
into the (possibly differently-ordered) loaded archive. If the archive's
names are all generic placeholders (meaning the archive itself carried no
name table, e.g. a bare/flagless GVMH), **no remapping is performed** — the
archive's natural load order is trusted as-is.

When a `.gj` model has **no** `GJTL` chunk at all (common for BML-extracted GJ
payloads, per the "GC BML texture index fix" quirk in §4.7), texture indices
found in strip material data are necessarily **absolute indices into the
loaded texture archive** (there is no name table to remap through), and the
bounds-check governing whether a `tid` is accepted must use
`len(self.textures)` rather than `len(self.texNames)` (which would always be
`0` in this no-GJTL case, incorrectly rejecting every texture reference).

---

## 11. GSL Archive Format

A minor auxiliary archive format supported by this importer for raw
extraction only (not further parsed/imported as models):

```
repeat until the read cursor reaches the lowest offset seen so far
(data_start, initialized to file_size and shrunk as entries are read):
    32 bytes  : name (ASCII, right-trimmed of space/tab/CR/LF/NUL)
    u32       : offset_sector   (multiply by 2048 to get the byte offset)
    u32       : length          (byte length of the entry's data)
    8 bytes   : padding, skipped
    (40 total bytes per table entry: 0x28 = 32+4+4, plus 8 bytes padding read separately)
    if name is empty: stop reading further entries (end of table)
    track offset = offset_sector * 2048; if offset < data_start: data_start = offset
```
The stopping condition for the table-read loop is `current_read_position <
data_start` — since `data_start` shrinks toward the true start of file data as
smaller offsets are discovered, this converges once the table has been fully
consumed and the read head reaches the beginning of the earliest data blob.
For each recorded entry, seek to `offset` and read `length` bytes verbatim
(no compression) — file data is extracted as-is to an output directory,
preserving the recorded `filename`.
