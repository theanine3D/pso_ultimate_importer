bl_info = {
    "name": "PSO Ultimate Importer",
    "author": "Theanine3D",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > PSO …",
    "description": (
        "Import Phantasy Star Online model and stage files: "
        "DC .nj / DC .rel (Dreamcast), "
        "BB .xj / BB n.rel (Blue Burst), "
        "GC .gj / GC n.rel (GameCube)"
    ),
    "category": "Import-Export",
}

import bpy
import math
import struct
import os
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty
from bpy.types import Operator

# ============================================================
# Magic numbers
# ============================================================
MAGIC_XVMH = 0x484d5658
MAGIC_XVRT = 0x54525658
MAGIC_NJCM = 0x4d434a4e   # NJ/XJ geometry chunk
MAGIC_NJTL = 0x4c544a4e   # NJ/XJ texture-name list chunk
MAGIC_NMDM = 0x4D444D4E   # NJ/XJ animation chunk (skipped)

# GameCube (Flipper) magic numbers
MAGIC_GVMH = 0x484D5647   # GVM texture archive header
MAGIC_GVRT = 0x54525647   # GVM individual texture
MAGIC_GJTL = 0x4C544A47   # GJ texture-name list chunk
MAGIC_GJCM = 0x4D434A47   # GJ geometry chunk

# POF0 relocation chunk (pointer-offset table; present in some NJ/GJ files)
MAGIC_POF0 = 0x30464F50   # 'POF0' little-endian

# ============================================================
# NJD Chunk constants (DC .nj and DC .rel formats)
# ============================================================
NJD_CN = 0;   NJD_CE = 255          # null / end

NJD_CB_BA=1; NJD_CB_DA=2; NJD_CB_EXP=3; NJD_CB_CP=4; NJD_CB_DP=5
CHUNK_BITS = [1,2,3,4,5]

NJD_CT_TID=8; NJD_CT_TID2=9
CHUNK_TINY = [8,9]

NJD_CM_D=17; NJD_CM_A=18; NJD_CM_DA=19; NJD_CM_S=20
NJD_CM_DS=21; NJD_CM_AS=22; NJD_CM_DAS=23
CHUNK_MATERIAL = [17,18,19,20,21,22,23]

NJD_CV_SH=32; NJD_CV_VN_SH=33; NJD_CV=34; NJD_CV_D8=35
NJD_CV_UF=36; NJD_CV_NF=37; NJD_CV_S5=38; NJD_CV_S4=39; NJD_CV_IN=40
NJD_CV_VN=41; NJD_CV_VN_D8=42; NJD_CV_VN_UF=43; NJD_CV_VN_NF=44
NJD_CV_VN_S5=45; NJD_CV_VN_S4=46; NJD_CV_VN_IN=47
NJD_CV_VNX=48; NJD_CV_VNX_D8=49; NJD_CV_VNX_UF=50
CHUNK_VERTEX = list(range(32, 51))

NJD_CO_P3=56; NJD_CO_P4=57; NJD_CO_ST=58
CHUNK_VOLUME = [56,57,58]

NJD_CS=64; NJD_CS_UVN=65; NJD_CS_UVH=66; NJD_CS_VN=67
NJD_CS_UVN_VN=68; NJD_CS_UVH_VN=69; NJD_CS_D8=70
NJD_CS_UVN_D8=71; NJD_CS_UVH_D8=72; NJD_CS_2=73
NJD_CS_UVN2=74; NJD_CS_UVH2=75
CHUNK_STRIP = list(range(64, 76))

# ============================================================
# Binary stream
# ============================================================
class BitStream:
    def __init__(self, data, big_endian=False):
        self.data = bytes(data)
        self.pos = 0
        self._e = '>' if big_endian else '<'

    def tell(self):
        return self.pos

    def getSize(self):
        return len(self.data)

    def seek(self, offset, whence=0):
        # whence 0 = absolute (NOESEEK_ABS), 1 = relative (NOESEEK_REL)
        if whence == 1:
            self.pos += offset
        else:
            self.pos = offset

    def readString(self):
        """Read a null-terminated ASCII string."""
        chars = []
        while self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            if b == 0:
                break
            chars.append(chr(b))
        return ''.join(chars)

    def readBytes(self, n):
        result = self.data[self.pos:self.pos + n]
        self.pos += n
        return result

    def readUInt(self):
        v, = struct.unpack_from(self._e + 'I', self.data, self.pos)
        self.pos += 4
        return v

    def readInt(self):
        v, = struct.unpack_from(self._e + 'i', self.data, self.pos)
        self.pos += 4
        return v

    def readUShort(self):
        v, = struct.unpack_from(self._e + 'H', self.data, self.pos)
        self.pos += 2
        return v

    def readShort(self):
        v, = struct.unpack_from(self._e + 'h', self.data, self.pos)
        self.pos += 2
        return v

    def readFloat(self):
        v, = struct.unpack_from(self._e + 'f', self.data, self.pos)
        self.pos += 4
        return v

    def readUByte(self):
        v, = struct.unpack_from('B', self.data, self.pos)
        self.pos += 1
        return v

# ============================================================
# DXT1 decoder
# ============================================================
def _rgb565(c):
    r = ((c >> 11) & 0x1F) * 255 // 31
    g = ((c >> 5)  & 0x3F) * 255 // 63
    b = ( c        & 0x1F) * 255 // 31
    return r, g, b

def decode_dxt1(data, width, height):
    """Decode a DXT1 (BC1) block-compressed texture to raw RGBA bytes (top-to-bottom)."""
    px = bytearray(width * height * 4)
    bw = max(1, (width  + 3) // 4)
    bh = max(1, (height + 3) // 4)
    p = 0
    for by in range(bh):
        for bx in range(bw):
            if p + 8 > len(data):
                break
            c0r, c1r = struct.unpack_from('<HH', data, p)
            bits     = struct.unpack_from('<I',  data, p + 4)[0]
            p += 8
            c0, c1 = _rgb565(c0r), _rgb565(c1r)
            if c0r > c1r:
                pal = [
                    c0 + (255,),
                    c1 + (255,),
                    tuple((2*c0[i] + c1[i]) // 3 for i in range(3)) + (255,),
                    tuple((c0[i] + 2*c1[i]) // 3 for i in range(3)) + (255,),
                ]
            else:
                pal = [
                    c0 + (255,),
                    c1 + (255,),
                    tuple((c0[i] + c1[i]) // 2 for i in range(3)) + (255,),
                    (0, 0, 0, 0),
                ]
            for py2 in range(4):
                for px2 in range(4):
                    col = pal[(bits >> (2 * (py2 * 4 + px2))) & 3]
                    x = bx * 4 + px2
                    y = by * 4 + py2
                    if x < width and y < height:
                        o = (y * width + x) * 4
                        px[o:o + 4] = col
    return bytes(px)

# XVM fmt2 values for block-compression formats
_XVM_FMT2_DXT1 = 6
_XVM_FMT2_DXT3 = 7
_XVM_FMT2_DXT5 = 8

def _dxt_color_block(data, p, px, bx, by, width, height,
                     force_4color=False, write_alpha=True):
    """Decode one 8-byte DXT color block into px at block position (bx,by).
    force_4color=True skips the transparent-index path (used by DXT3/DXT5).
    write_alpha=False leaves the alpha channel untouched (DXT3/DXT5 set it
    from their own alpha block before calling this)."""
    c0r, c1r = struct.unpack_from('<HH', data, p)
    bits      = struct.unpack_from('<I',  data, p + 4)[0]
    c0, c1    = _rgb565(c0r), _rgb565(c1r)
    if force_4color or c0r > c1r:
        pal = [
            c0 + (255,),
            c1 + (255,),
            tuple((2*c0[i] + c1[i]) // 3 for i in range(3)) + (255,),
            tuple((c0[i] + 2*c1[i]) // 3 for i in range(3)) + (255,),
        ]
    else:
        pal = [
            c0 + (255,),
            c1 + (255,),
            tuple((c0[i] + c1[i]) // 2 for i in range(3)) + (255,),
            (0, 0, 0, 0),
        ]
    for py2 in range(4):
        for px2 in range(4):
            col = pal[(bits >> (2 * (py2 * 4 + px2))) & 3]
            x = bx * 4 + px2; y = by * 4 + py2
            if x < width and y < height:
                o = (y * width + x) * 4
                px[o], px[o+1], px[o+2] = col[0], col[1], col[2]
                if write_alpha:
                    px[o+3] = col[3]

def decode_dxt3(data, width, height):
    """Decode a DXT3 (BC2) texture to raw RGBA bytes (top-to-bottom).
    Each 16-byte block: 8 bytes explicit 4-bit alpha, 8 bytes DXT1 color."""
    px = bytearray(width * height * 4)
    bw = max(1, (width  + 3) // 4)
    bh = max(1, (height + 3) // 4)
    p  = 0
    for by in range(bh):
        for bx in range(bw):
            if p + 16 > len(data):
                break
            # Alpha block: 8 bytes, 4 bits per pixel, rows LSB-first
            for py2 in range(4):
                row_a = struct.unpack_from('<H', data, p + py2 * 2)[0]
                for px2 in range(4):
                    x = bx * 4 + px2; y = by * 4 + py2
                    if x < width and y < height:
                        a = ((row_a >> (px2 * 4)) & 0xF) * 17  # 0xF→255, 0→0
                        px[(y * width + x) * 4 + 3] = a
            _dxt_color_block(data, p + 8, px, bx, by, width, height,
                             force_4color=True, write_alpha=False)
            p += 16
    return bytes(px)

def decode_dxt5(data, width, height):
    """Decode a DXT5 (BC3) texture to raw RGBA bytes (top-to-bottom).
    Each 16-byte block: 2 alpha refs + 6 bytes alpha indices, 8 bytes DXT1 color."""
    px = bytearray(width * height * 4)
    bw = max(1, (width  + 3) // 4)
    bh = max(1, (height + 3) // 4)
    p  = 0
    for by in range(bh):
        for bx in range(bw):
            if p + 16 > len(data):
                break
            a0, a1 = data[p], data[p + 1]
            # 6 bytes = 48 bits of 3-bit indices for 16 pixels
            abits = int.from_bytes(data[p + 2: p + 8], 'little')
            if a0 > a1:
                apal = [a0, a1,
                        (6*a0 + 1*a1) // 7, (5*a0 + 2*a1) // 7,
                        (4*a0 + 3*a1) // 7, (3*a0 + 4*a1) // 7,
                        (2*a0 + 5*a1) // 7, (1*a0 + 6*a1) // 7]
            else:
                apal = [a0, a1,
                        (4*a0 + 1*a1) // 5, (3*a0 + 2*a1) // 5,
                        (2*a0 + 3*a1) // 5, (1*a0 + 4*a1) // 5,
                        0, 255]
            for py2 in range(4):
                for px2 in range(4):
                    x = bx * 4 + px2; y = by * 4 + py2
                    if x < width and y < height:
                        idx = (abits >> (3 * (py2 * 4 + px2))) & 7
                        px[(y * width + x) * 4 + 3] = apal[idx]
            _dxt_color_block(data, p + 8, px, bx, by, width, height,
                             force_4color=True, write_alpha=False)
            p += 16
    return bytes(px)

# ============================================================
# XVM texture archive loader
# Returns a list of dicts: {name, width, height, pixels (RGBA bytes)}
# ============================================================
def xvr_load(data):
    bs = BitStream(data)
    if bs.readUInt() != MAGIC_XVMH:
        return []
    bs.readUInt()       # archive length
    bs.readUInt()       # texture count (we just collect what we find)

    tex_offsets = []
    while bs.tell() < bs.getSize() - 4:
        magic = bs.readUInt()
        if magic == MAGIC_XVRT:
            bs.readUInt()               # chunk length
            tex_offsets.append(bs.tell())

    textures = []
    for ofs in tex_offsets:
        bs.seek(ofs)
        bs.readUInt()               # format_1 (color format, unused for decoding)
        fmt2   = bs.readUInt()      # format_2 (compression type: 6=DXT1, 7=DXT3, 8=DXT5)
        bs.readUInt()               # tex_id (index within archive)
        width  = bs.readUShort()
        height = bs.readUShort()
        size   = bs.readUInt()
        bs.readBytes(0x24)          # padding / header tail
        raw    = bs.readBytes(size)
        if fmt2 == _XVM_FMT2_DXT3:
            pixels = decode_dxt3(raw, width, height)
        elif fmt2 == _XVM_FMT2_DXT5:
            pixels = decode_dxt5(raw, width, height)
        else:
            pixels = decode_dxt1(raw, width, height)   # fmt2=6 or unknown → DXT1
        textures.append({
            'name':   "Texture_%d" % len(textures),
            'width':  width,
            'height': height,
            'pixels': pixels,
        })
    return textures

# ============================================================
# PowerVR (PVM / PVR) texture decoder — for DC .nj / DC .rel
# ============================================================
def _pvr_untwiddle(x, y):
    def ut(v):
        r = 0
        for i in range(10):
            b = 1 << i
            if v & b: r |= b << i
        return r
    return ut(y) | (ut(x) << 1)

def _pvr_color(fmt, v):
    if fmt == 0:    # ARGB_1555
        a = 255 if (v & 0x8000) else 0
        r = ((v >> 10) & 0x1F) * 255 // 31
        g = ((v >>  5) & 0x1F) * 255 // 31
        b = ( v        & 0x1F) * 255 // 31
        return (r, g, b, a)
    elif fmt == 1:  # RGB_565
        r = ((v >> 11) & 0x1F) * 255 // 31
        g = ((v >>  5) & 0x3F) * 255 // 63
        b = ( v        & 0x1F) * 255 // 31
        return (r, g, b, 255)
    elif fmt == 2:  # ARGB_4444
        a = ((v >> 12) & 0xF) * 255 // 15
        r = ((v >>  8) & 0xF) * 255 // 15
        g = ((v >>  4) & 0xF) * 255 // 15
        b = ( v        & 0xF) * 255 // 15
        if (r or g or b) and a == 0: a = 255
        return (r, g, b, a)
    return (128, 128, 128, 255)

def _pvr_mipmap_skip(width, height, compressed):
    """Return byte count to skip past all mipmap levels below full size."""
    skip = 0
    mip = 0; w = width
    while w: mip += 1; w >>= 1
    while mip:
        mw = width  >> (mip - 1)
        mh = height >> (mip - 1)
        mip -= 1
        if mip > 0:
            skip += (mw * mh // 4) if compressed else (mw * mh * 2)
        else:
            skip += 1 if compressed else 2
    return skip

def decode_pvrt(raw, color_fmt, data_fmt, width, height):
    """Decode a single PVR texture payload to RGBA bytes (top-to-bottom)."""
    TWIDDLED  = (0x01, 0x02, 0x0D, 0x12)
    VQ        = (0x03, 0x04, 0x10, 0x11)
    RECTANGLE = (0x09,)
    HAS_MIPS  = (0x02, 0x04, 0x06, 0x08, 0x0F, 0x11, 0x12)

    px   = bytearray(width * height * 4)
    pos  = 0
    compressed = data_fmt in VQ

    if data_fmt in HAS_MIPS:
        pos += _pvr_mipmap_skip(width, height, compressed)

    if data_fmt in VQ:
        cb_size = 256
        if data_fmt in (0x10, 0x11):   # SMALLVQ
            if width <= 16: cb_size = 16
            elif width == 32: cb_size = 32
            elif width == 64: cb_size = 128
        codebook = []
        for _ in range(cb_size):
            entry = []
            for _ in range(4):
                if pos + 2 > len(raw): entry.append((0,0,0,255)); continue
                v, = struct.unpack_from('<H', raw, pos); pos += 2
                entry.append(_pvr_color(color_fmt, v))
            codebook.append(entry)
        idx_start = pos
        for y in range(height // 2):
            for x in range(width // 2):
                i = _pvr_untwiddle(x, y)
                p = idx_start + i
                if p >= len(raw): continue
                quad = codebook[raw[p] % len(codebook)]
                for qy in range(2):
                    for qx in range(2):
                        ox, oy = x*2+qx, y*2+qy
                        if ox < width and oy < height:
                            o = (oy * width + ox) * 4
                            px[o:o+4] = quad[qy*2+qx]

    elif data_fmt in TWIDDLED:
        for y in range(height):
            for x in range(width):
                i = _pvr_untwiddle(x, y)
                bp = pos + i * 2
                if bp + 2 > len(raw): continue
                v, = struct.unpack_from('<H', raw, bp)
                o = (y * width + x) * 4
                px[o:o+4] = _pvr_color(color_fmt, v)

    elif data_fmt in RECTANGLE:
        for y in range(height):
            for x in range(width):
                if pos + 2 > len(raw): break
                v, = struct.unpack_from('<H', raw, pos); pos += 2
                o = (y * width + x) * 4
                px[o:o+4] = _pvr_color(color_fmt, v)

    return bytes(px)

def pvm_load(data):
    """Load a .pvm PowerVR texture archive. Returns list of texture dicts."""
    textures = []
    pos = 0

    # Scan for PVMH or a bare PVRT
    while pos <= len(data) - 4:
        tag = data[pos:pos+4]
        if tag == b'PVMH':
            pos += 4; break
        elif tag == b'PVRT':
            # Single PVR file
            pos += 8  # skip PVRT + length
            if pos + 8 > len(data): return textures
            cf, df = data[pos], data[pos+1]
            w, h = struct.unpack_from('<HH', data, pos+4)
            pixels = decode_pvrt(data[pos+8:], cf, df, w, h)
            textures.append({'name': 'texture_000', 'width': w, 'height': h, 'pixels': pixels})
            return textures
        pos += 1
    if pos > len(data) - 4:
        return textures

    pvmh_len, = struct.unpack_from('<I', data, pos); pos += 4
    save = pos
    flags, tex_count = struct.unpack_from('<HH', data, pos); pos += 4

    entries = []
    for i in range(tex_count):
        idx, = struct.unpack_from('<H', data, pos); pos += 2
        e = {'index': idx, 'name': "texture_%03d" % idx}
        if flags & 0x08:
            raw = data[pos:pos+0x1c]; pos += 0x1c
            e['name'] = raw.decode('ascii', errors='ignore').rstrip('\x00')
        if flags & 0x04: pos += 2
        if flags & 0x02:
            sv, = struct.unpack_from('<H', data, pos); pos += 2
            e['width']  = 1 << ((sv & 0x0f) + 2)
            e['height'] = 1 << (((sv >> 4) & 0x0f) + 2)
        if flags & 0x01:
            e['index'], = struct.unpack_from('<I', data, pos); pos += 4
        entries.append(e)

    # Start PVRT scan from wherever the entry-parsing left off.
    # Using 'pos' (current read head) is more reliable than 'save + pvmh_len'
    # because some DC PVM files count pvmh_len from the start of the PVMH block
    # (including the 8-byte magic+length prefix) rather than from after it,
    # which would cause save+pvmh_len to overshoot the first PVRT by 8 bytes.
    # If there is any remaining PVMH padding, take whichever is larger.
    rpos = max(pos, save + pvmh_len)
    for e in entries:
        while rpos <= len(data) - 4:
            if data[rpos:rpos+4] == b'PVRT': rpos += 4; break
            rpos += 1
        else: break
        plen, = struct.unpack_from('<I', data, rpos); rpos += 4
        dat_start = rpos; rpos += plen
        if dat_start + 8 > len(data): continue
        cf, df = data[dat_start], data[dat_start+1]
        w, h = struct.unpack_from('<HH', data, dat_start+4)
        raw_tex = data[dat_start+8:]
        pixels = decode_pvrt(raw_tex, cf, df, w, h)
        name = os.path.splitext(e['name'])[0] if '.' in e['name'] else e['name']
        if not name: name = "texture_%03d" % len(textures)
        textures.append({'name': name, 'width': w, 'height': h, 'pixels': pixels})

    return textures


def load_texture_archive(data):
    """Auto-detect XVM / GVM / PVM by magic and call the right loader."""
    if len(data) < 4:
        return []
    magic = data[0:4]
    if magic in (b'XVMH', b'XVRT'):
        return xvr_load(data)
    if magic in (b'GVMH', b'GVRT'):
        return gvm_load(data)
    return pvm_load(data)


# ============================================================
# GameCube (GVR / GVM) texture decoder
# GVR pixel data is stored in hardware-native tiled layouts.
# ============================================================

def _gc_rgb565c(v):
    """Return (r, g, b) from a big-endian GC RGB565 value."""
    return (((v >> 11) & 0x1F) * 255 // 31,
            ((v >>  5) & 0x3F) * 255 // 63,
            ( v        & 0x1F) * 255 // 31)

def _gc_rgb565(v):
    r, g, b = _gc_rgb565c(v)
    return (r, g, b, 255)

def _gc_rgb5a3(v):
    if v & 0x8000:   # RGB555, fully opaque
        r = ((v >> 10) & 0x1F) * 255 // 31
        g = ((v >>  5) & 0x1F) * 255 // 31
        b = ( v        & 0x1F) * 255 // 31
        return (r, g, b, 255)
    else:            # RGB4A3
        a = ((v >> 12) & 0x7) * 255 // 7
        r = ((v >>  8) & 0xF) * 255 // 15
        g = ((v >>  4) & 0xF) * 255 // 15
        b = ( v        & 0xF) * 255 // 15
        return (r, g, b, a)


def decode_gvr(data, pixel_fmt, data_fmt, width, height):
    """Decode a GVR texture payload to raw RGBA bytes (top-to-bottom)."""
    if width == 0 or height == 0:
        return b''
    px = bytearray(width * height * 4)

    def put(x, y, rgba):
        if x < width and y < height:
            o = (y * width + x) * 4
            px[o]   = rgba[0]; px[o+1] = rgba[1]
            px[o+2] = rgba[2]; px[o+3] = rgba[3]

    pos = 0
    n   = len(data)

    if data_fmt == 0x00:          # I4 — 8×8 tiles, 4 bpp
        for by in range(0, height, 8):
            for bx in range(0, width, 8):
                for ty in range(8):
                    for tx in range(0, 8, 2):
                        if pos >= n: break
                        b = data[pos]; pos += 1
                        i0 = (b >> 4) * 17;  i1 = (b & 0xF) * 17
                        put(bx+tx,   by+ty, (i0, i0, i0, 255))
                        put(bx+tx+1, by+ty, (i1, i1, i1, 255))

    elif data_fmt == 0x01:        # I8 — 8×4 tiles, 8 bpp
        for by in range(0, height, 4):
            for bx in range(0, width, 8):
                for ty in range(4):
                    for tx in range(8):
                        if pos >= n: break
                        i = data[pos]; pos += 1
                        put(bx+tx, by+ty, (i, i, i, 255))

    elif data_fmt == 0x02:        # IA4 — 8×4 tiles, 8 bpp (I=hi nibble, A=lo)
        for by in range(0, height, 4):
            for bx in range(0, width, 8):
                for ty in range(4):
                    for tx in range(8):
                        if pos >= n: break
                        b = data[pos]; pos += 1
                        i = (b >> 4) * 17;  a = (b & 0xF) * 17
                        put(bx+tx, by+ty, (i, i, i, a))

    elif data_fmt == 0x03:        # IA8 — 4×4 tiles, 16 bpp BE (A=hi, I=lo)
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                for ty in range(4):
                    for tx in range(4):
                        if pos + 2 > n: break
                        v = struct.unpack_from('>H', data, pos)[0]; pos += 2
                        a = (v >> 8) & 0xFF;  i = v & 0xFF
                        put(bx+tx, by+ty, (i, i, i, a))

    elif data_fmt == 0x04:        # RGB565 — 4×4 tiles, 16 bpp BE
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                for ty in range(4):
                    for tx in range(4):
                        if pos + 2 > n: break
                        v = struct.unpack_from('>H', data, pos)[0]; pos += 2
                        put(bx+tx, by+ty, _gc_rgb565(v))

    elif data_fmt == 0x05:        # RGB5A3 — 4×4 tiles, 16 bpp BE
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                for ty in range(4):
                    for tx in range(4):
                        if pos + 2 > n: break
                        v = struct.unpack_from('>H', data, pos)[0]; pos += 2
                        put(bx+tx, by+ty, _gc_rgb5a3(v))

    elif data_fmt == 0x06:        # RGBA8 — 4×4 tiles, AR sub-block then GB sub-block
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                ar = data[pos:pos+32]; gb = data[pos+32:pos+64]; pos += 64
                for ty in range(4):
                    for tx in range(4):
                        k = (ty * 4 + tx) * 2
                        a = ar[k] if k   < len(ar) else 255
                        r = ar[k+1] if k+1 < len(ar) else 0
                        g = gb[k]   if k   < len(gb) else 0
                        b = gb[k+1] if k+1 < len(gb) else 0
                        put(bx+tx, by+ty, (r, g, b, a))

    elif data_fmt == 0x0E:        # CMPR (DXT1 variant) — 8×8 super-tiles
        # Each 8×8 super-tile holds 2×2 DXT1 sub-blocks (top-left, top-right,
        # bottom-left, bottom-right).  GC DXT1 color endpoints are big-endian;
        # the 4-byte index table uses MSB-first bit order within each row byte.
        for by in range(0, height, 8):
            for bx in range(0, width, 8):
                for sy in range(2):
                    for sx in range(2):
                        if pos + 8 > n: pos += 8; continue
                        c0v = struct.unpack_from('>H', data, pos)[0]
                        c1v = struct.unpack_from('>H', data, pos+2)[0]
                        idx_tbl = data[pos+4:pos+8]; pos += 8
                        r0,g0,b0 = _gc_rgb565c(c0v)
                        r1,g1,b1 = _gc_rgb565c(c1v)
                        if c0v > c1v:
                            pal = [(r0,g0,b0,255),(r1,g1,b1,255),
                                   ((2*r0+r1)//3,(2*g0+g1)//3,(2*b0+b1)//3,255),
                                   ((r0+2*r1)//3,(g0+2*g1)//3,(b0+2*b1)//3,255)]
                        else:
                            pal = [(r0,g0,b0,255),(r1,g1,b1,255),
                                   ((r0+r1)//2,(g0+g1)//2,(b0+b1)//2,255),
                                   (0,0,0,0)]
                        for ty in range(4):
                            row = idx_tbl[ty] if ty < len(idx_tbl) else 0
                            for tx in range(4):
                                # MSB-first: pixel 0 in bits [7:6]
                                idx = (row >> (6 - tx * 2)) & 3
                                put(bx + sx*4 + tx, by + sy*4 + ty, pal[idx])

    else:
        # Unsupported format — fill with magenta so it's obviously missing
        for y in range(height):
            for x in range(width):
                put(x, y, (255, 0, 255, 255))

    return bytes(px)


def gvm_load(data):
    """Load a .gvm GameCube texture archive. Returns list of texture dicts."""
    textures = []

    # --- Find GVMH magic (LE scan matches how PSO GVM files are laid out) ---
    pos = 0
    while pos <= len(data) - 4:
        if data[pos:pos+4] == b'GVMH': break
        if data[pos:pos+4] == b'GVRT':
            # Bare single-texture GVR file
            pos += 4
            chunk_len = struct.unpack_from('<I', data, pos)[0]; pos += 4
            if pos + 8 > len(data): return textures
            pixel_fmt = data[pos+2] >> 4
            data_fmt  = data[pos+3]
            w, h      = struct.unpack_from('>HH', data, pos+4)
            pix = decode_gvr(data[pos+8:pos+chunk_len], pixel_fmt, data_fmt, w, h)
            textures.append({'name': 'texture_000', 'width': w, 'height': h, 'pixels': pix})
            return textures
        pos += 1
    else:
        return textures  # nothing found

    pos += 4  # skip 'GVMH'
    chunk_len = struct.unpack_from('<I', data, pos)[0]; pos += 4
    gvmh_end  = pos + chunk_len

    # --- Parse GVMH entry table (big-endian content) ---
    flags, tex_count = struct.unpack_from('>HH', data, pos); pos += 4
    header_entries = []
    for i in range(tex_count):
        if pos + 2 > len(data): break
        idx = struct.unpack_from('>H', data, pos)[0]; pos += 2
        e = {'index': idx, 'name': "texture_%03d" % idx}
        if flags & 0x08:
            raw = data[pos:pos+0x1c]; pos += 0x1c
            e['name'] = raw.decode('ascii', errors='ignore').rstrip('\x00')
        if flags & 0x04:
            pos += 2    # pixel-format field (unused for decoding)
        if flags & 0x02:
            sz = struct.unpack_from('>H', data, pos)[0]; pos += 2
            e['width']  = 1 << ((sz & 0x0f) + 2)
            e['height'] = 1 << (((sz >> 4) & 0x0f) + 2)
        if flags & 0x01:
            pos += 4    # GUID / index field
        header_entries.append(e)

    # --- Scan for GVRT chunks and decode each one ---
    scan = max(pos, gvmh_end)
    for e in header_entries:
        while scan <= len(data) - 4:
            if data[scan:scan+4] == b'GVRT': break
            scan += 1
        else: break
        scan += 4  # skip 'GVRT'
        plen  = struct.unpack_from('<I', data, scan)[0]; scan += 4
        pstart = scan; scan += plen
        if pstart + 8 > len(data): continue

        pixel_fmt = data[pstart+2] >> 4
        data_fmt  = data[pstart+3]
        w, h      = struct.unpack_from('>HH', data, pstart+4)
        raw_pix   = data[pstart+8 : pstart+plen]
        pix       = decode_gvr(raw_pix, pixel_fmt, data_fmt, w, h)
        name = os.path.splitext(e['name'])[0] if '.' in e['name'] else e['name']
        if not name: name = "texture_%03d" % len(textures)
        textures.append({'name': name, 'width': w, 'height': h, 'pixels': pix})

    return textures

# ============================================================
# Matrix 4x4 (DashMat4 from original, extended with transformPoint /
# transformNormal so we can work without Noesis vector types)
# ============================================================
class DashMat4:

    def __init__(self):
        self.mtx = self._id()

    @staticmethod
    def _id():
        return [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]

    def _mul44(self, a, b):
        t = self._id()
        for i in range(4):
            for j in range(4):
                s = 0.0
                for k in range(4):
                    s += a[i][k] * b[k][j]
                t[i][j] = s
        return t

    def multiply(self, factor):
        self.mtx = self._mul44(self.mtx, factor)

    def scale(self, v):
        t = self._id()
        t[0][0] = v[0]; t[1][1] = v[1]; t[2][2] = v[2]
        self.multiply(t)

    def translate(self, v):
        t = self._id()
        t[3][0] = v[0]; t[3][1] = v[1]; t[3][2] = v[2]
        self.multiply(t)

    def rotate(self, v):
        x, y, z = v
        t = self._id(); c, s = math.cos(x), math.sin(x)
        t[1][1]=c; t[1][2]=s; t[2][1]=-s; t[2][2]=c
        self.multiply(t)
        t = self._id(); c, s = math.cos(y), math.sin(y)
        t[0][0]=c; t[0][2]=-s; t[2][0]=s; t[2][2]=c
        self.multiply(t)
        t = self._id(); c, s = math.cos(z), math.sin(z)
        t[0][0]=c; t[0][1]=s; t[1][0]=-s; t[1][1]=c
        self.multiply(t)

    def rotate4(self, vec3, w):
        """Quaternion (x,y,z,w) → rotation matrix (used by DC .nj bones)."""
        x, y, z = vec3
        x2, y2, z2 = x+x, y+y, z+z
        xx, xy, xz = x*x2, x*y2, x*z2
        yy, yz, zz = y*y2, y*z2, z*z2
        wx, wy, wz = w*x2, w*y2, w*z2
        t = self._id()
        t[0][0]=1-(yy+zz); t[0][1]=xy+wz;    t[0][2]=xz-wy
        t[1][0]=xy-wz;     t[1][1]=1-(xx+zz); t[1][2]=yz+wx
        t[2][0]=xz+wy;     t[2][1]=yz-wx;     t[2][2]=1-(xx+yy)
        self.multiply(t)

    def compose(self, parent):
        """Post-multiply by parent (equivalent to NoeMat43 *= parent)."""
        self.mtx = self._mul44(self.mtx, parent.mtx)

    def copy(self):
        r = DashMat4()
        r.mtx = [row[:] for row in self.mtx]
        return r

    def transformPoint(self, p):
        m = self.mtx
        return (
            p[0]*m[0][0] + p[1]*m[1][0] + p[2]*m[2][0] + m[3][0],
            p[0]*m[0][1] + p[1]*m[1][1] + p[2]*m[2][1] + m[3][1],
            p[0]*m[0][2] + p[1]*m[1][2] + p[2]*m[2][2] + m[3][2],
        )

    def transformNormal(self, n):
        m = self.mtx
        return (
            n[0]*m[0][0] + n[1]*m[1][0] + n[2]*m[2][0],
            n[0]*m[0][1] + n[1]*m[1][1] + n[2]*m[2][1],
            n[0]*m[0][2] + n[1]*m[1][2] + n[2]*m[2][2],
        )

# ============================================================
# Ninja Stage Geometry parser
# Output: self.meshes_data, self.materials_data, self.textures
# ============================================================
class NinjaStageGeometry:

    def __init__(self):
        self.meshes_data    = []   # list of mesh dicts
        self.materials_data = []   # list of material dicts
        self.textures       = []   # list from xvr_load()
        self.matrix         = DashMat4()

    def setTextures(self, textures):
        self.textures = textures

    # ------------------------------------------------------------------
    def parse(self, data):
        self.bs = BitStream(data)

        # Footer: last 16 bytes, first uint is the table offset
        self.bs.seek(self.bs.getSize() - 16)
        tableOfs = self.bs.readUInt()

        self.bs.seek(tableOfs)
        self.bs.readUInt()          # fmt2
        n_count = self.bs.readUInt()
        d_count = self.bs.readUInt()
        self.bs.readUInt()          # hd
        d_ofs   = self.bs.readUInt()
        self.bs.readUInt()          # n_ofs (unused by original)

        c = 2.0 * math.pi / 0x10000

        # Read D-section descriptors
        self.bs.seek(d_ofs)
        d_sections = []
        for _ in range(d_count):
            section_id = self.bs.readInt()
            section = {
                'id':           section_id,
                'pos':          [self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()],
                'rot':          (self.bs.readInt()*c, self.bs.readInt()*c, self.bs.readInt()*c),
                'radius':       self.bs.readFloat(),
                'static_ofs':   self.bs.readUInt(),
                'animated_ofs': self.bs.readUInt(),
                'static_num':   self.bs.readUInt(),
                'animated_num': self.bs.readUInt(),
                'end':          self.bs.readUInt(),
            }
            d_sections.append(section)

        for section in d_sections:
            mat = DashMat4()
            mat.rotate(section['rot'])
            mat.translate(section['pos'])

            mesh_offsets = []
            self.bs.seek(section['static_ofs'])
            for _ in range(section['static_num']):
                mesh_offsets.append(self.bs.readUInt())
                self.bs.readBytes(0x0C)

            for ofs in mesh_offsets:
                self.bs.seek(ofs)
                self.readNode(mat)

    # ------------------------------------------------------------------
    def readNode(self, pMatrix=None):
        if self.bs.pos + 52 > self.bs.getSize():
            return
        c = 2.0 * math.pi / 0x10000
        node = {
            'flags':      self.bs.readUInt(),
            'meshOfs':    self.bs.readUInt(),
            'pos':        (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
            'rot':        (self.bs.readInt()*c, self.bs.readInt()*c, self.bs.readInt()*c),
            'scl':        (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
            'childOfs':   self.bs.readUInt(),
            'siblingOfs': self.bs.readUInt(),
        }

        mat = DashMat4()
        if not (node['flags'] & 0x02):
            mat.rotate(node['rot'])
        if not (node['flags'] & 0x01):
            mat.translate(node['pos'])

        if pMatrix is not None:
            mat.compose(pMatrix)

        self.matrix = mat

        size = self.bs.getSize()
        if node['meshOfs'] >= size or node['childOfs'] >= size or node['siblingOfs'] >= size:
            return

        if node['meshOfs'] != 0:
            self.bs.seek(node['meshOfs'])
            self.readMesh()

        if node['childOfs'] != 0:
            self.bs.seek(node['childOfs'])
            self.readNode(mat)

        if node['siblingOfs'] != 0:
            self.bs.seek(node['siblingOfs'])
            self.readNode(pMatrix)

    # ------------------------------------------------------------------
    def readMesh(self):
        mesh = {
            'flags':                         self.bs.readUInt(),
            'vertex_info_list_offset':       self.bs.readUInt(),
            'vertex_info_count':             self.bs.readUInt(),
            'triangle_strip_list_a_offset':  self.bs.readUInt(),
            'triangle_strip_a_count':        self.bs.readUInt(),
            'triangle_strip_list_b_offset':  self.bs.readUInt(),
            'triangle_strip_b_count':        self.bs.readUInt(),
            'center': (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
            'radius':                        self.bs.readFloat(),
        }

        sz = self.bs.getSize()
        vlo = mesh['vertex_info_list_offset']
        if vlo and vlo < sz:
            self.bs.seek(vlo)
            self.readVertexList()

        if mesh['triangle_strip_a_count']:
            aso = mesh['triangle_strip_list_a_offset']
            if aso and aso < sz:
                self.bs.seek(aso)
                self.readStripList(mesh['triangle_strip_a_count'], False)

        if mesh['triangle_strip_b_count']:
            bso = mesh['triangle_strip_list_b_offset']
            if bso and bso < sz:
                self.bs.seek(bso)
                self.readStripList(mesh['triangle_strip_b_count'], True)

    # ------------------------------------------------------------------
    def readVertexList(self):
        vtype  = self.bs.readUShort()
        self.bs.readUShort()        # unknown
        vofs   = self.bs.readUInt()
        self.bs.readUInt()          # vertex_size
        vcount = self.bs.readUInt()

        read_uv     = bool(vtype & 0x01)
        read_normal = bool(vtype & 0x02)
        read_color  = bool(vtype & 0x04)

        sz = self.bs.getSize()
        if not vofs or vofs >= sz:
            return
        self.bs.seek(vofs)
        self.vertex_stack = {}

        for i in range(vcount):
            vertex = {'pos': None, 'norm': None, 'color': None, 'uv': None}

            p = (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat())
            vertex['pos'] = self.matrix.transformPoint(p)

            if read_normal:
                n = (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat())
                vertex['norm'] = self.matrix.transformNormal(n)

            if read_color:
                # File byte order is BGRA (common for DC/GC-era Dreamcast hardware)
                b2 = self.bs.readUByte() / 255.0
                g2 = self.bs.readUByte() / 255.0
                r2 = self.bs.readUByte() / 255.0
                a2 = self.bs.readUByte() / 255.0
                vertex['color'] = (r2, g2, b2, a2)

            if read_uv:
                u = self.bs.readFloat()
                v = self.bs.readFloat()
                vertex['uv'] = (u, 1.0 - v)

            self.vertex_stack[i] = vertex

    # ------------------------------------------------------------------
    def readStripList(self, count, useAlpha):
        strip_info = []
        for _ in range(count):
            strip_info.append({
                'material_property_list_offset': self.bs.readUInt(),
                'material_property_list_size':   self.bs.readUInt(),
                'index_list_offset':             self.bs.readUInt(),
                'index_count':                   self.bs.readUInt(),
                'unknown':                       self.bs.readUInt(),
            })

        self.material = {
            'diffuse':    (1.0, 1.0, 1.0, 1.0),
            'ambient':    (1.0, 1.0, 1.0, 1.0),
            'specular':   (1.0, 1.0, 1.0, 1.0),
            'texIndex':   -1,
            'blendSrc':   '',
            'blendDst':   '',
            'doubleSided': False,
        }

        for strip in strip_info:
            self.bs.seek(strip['material_property_list_offset'])
            self.readMaterial(strip['material_property_list_size'])

            points = []
            self.bs.seek(strip['index_list_offset'])
            for _ in range(strip['index_count']):
                points.append(self.bs.readShort())

            clockwise = False
            triangles = []
            for i in range(len(points) - 2):
                if clockwise:
                    a, b, c = points[i], points[i+2], points[i+1]
                else:
                    a, b, c = points[i+1], points[i+2], points[i]
                clockwise = not clockwise
                if a != b and b != c and c != a:
                    triangles.extend([a, b, c])

            self.appendMesh(triangles)

    # ------------------------------------------------------------------
    def readMaterial(self, prop_count):
        for _ in range(prop_count):
            mat_type = self.bs.readUInt()
            if mat_type == 2:
                dst = self.bs.readUInt()
                src = self.bs.readUInt()
                if src == 1 and dst == 4:
                    self.material['blendSrc'] = 'ONE'
                    self.material['blendDst'] = 'ONE'
                elif src == 5 and dst == 4:
                    self.material['blendSrc'] = ''
                    self.material['blendDst'] = ''
                self.bs.readBytes(4)
            elif mat_type == 3:
                tex_id = self.bs.readUInt()
                self.bs.readBytes(8)
                self.material['texIndex'] = tex_id
            elif mat_type == 4:
                self.bs.readBytes(12)
                self.material['doubleSided'] = True
            elif mat_type == 5:
                r = self.bs.readUByte() / 255.0
                g = self.bs.readUByte() / 255.0
                b = self.bs.readUByte() / 255.0
                a = self.bs.readUByte() / 255.0
                self.material['diffuse'] = (r, g, b, a)
                self.bs.readBytes(8)
            else:
                self.bs.readBytes(12)

    # ------------------------------------------------------------------
    def appendMesh(self, triangles):
        if not triangles:
            return

        pos_list   = []
        norm_list  = []
        color_list = []
        uv_list    = []
        tri_list   = []

        for point in triangles:
            if point not in self.vertex_stack:
                continue
            vert = self.vertex_stack[point]
            tri_list.append(len(pos_list))
            pos_list.append(vert['pos'])
            if vert['norm']  is not None: norm_list.append(vert['norm'])
            if vert['color'] is not None: color_list.append(vert['color'])
            if vert['uv']    is not None: uv_list.append(vert['uv'])

        if not pos_list:
            return

        has_colors = bool(color_list)

        # De-duplicate or create material entry
        # has_colors is part of the key so meshes with/without vertex colors
        # get separate materials (avoids black output when "Col" attr is absent)
        mat_key = (
            self.material['diffuse'],
            self.material['texIndex'],
            self.material['blendSrc'],
            self.material['blendDst'],
            has_colors,
        )
        mat_index = next(
            (i for i, m in enumerate(self.materials_data) if m['key'] == mat_key),
            None
        )
        if mat_index is None:
            mat_index = len(self.materials_data)
            self.materials_data.append({
                'key':              mat_key,
                'name':             "mat_%03d" % mat_index,
                'diffuse':          self.material['diffuse'],
                'texIndex':         self.material['texIndex'],
                'blendSrc':         self.material['blendSrc'],
                'blendDst':         self.material['blendDst'],
                'doubleSided':      self.material['doubleSided'],
                'has_vertex_colors': has_colors,
            })

        self.meshes_data.append({
            'positions': pos_list,
            'normals':   norm_list,
            'colors':    color_list,
            'uvs':       uv_list,
            'triangles': tri_list,
            'mat_index': mat_index,
        })

# ============================================================
# Build Blender scene from parsed geometry
# ============================================================
def build_blender_scene(geo, filepath, blend_vertex_colors=True):
    # --- Textures -> Blender images ---
    bl_images = []
    bl_images_has_alpha = []   # True if any pixel has alpha < 255
    bl_images_is_solid  = []   # True if every pixel is identical (single solid color)
    for tex in geo.textures:
        img = bpy.data.images.new(tex['name'], tex['width'], tex['height'], alpha=True)
        w, h = tex['width'], tex['height']
        raw = tex['pixels']
        # Blender pixel buffer is RGBA floats, row 0 at the bottom, so flip Y
        floats = []
        has_alpha  = False
        is_solid   = True
        first_px   = raw[0:4] if len(raw) >= 4 else None
        for y in range(h - 1, -1, -1):
            for x in range(w):
                o = (y * w + x) * 4
                a = raw[o + 3]
                if a < 255:
                    has_alpha = True
                if is_solid and first_px and raw[o:o+4] != first_px:
                    is_solid = False
                floats += [raw[o]/255.0, raw[o+1]/255.0, raw[o+2]/255.0, a/255.0]
        img.pixels[:] = floats
        img.pack()
        bl_images.append(img)
        bl_images_has_alpha.append(has_alpha)
        bl_images_is_solid.append(is_solid)

    # --- Materials -> Blender materials ---
    bl_materials = []
    for md in geo.materials_data:
        mat = bpy.data.materials.new(name=md['name'])
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()

        use_vc_blend = blend_vertex_colors and md.get('has_vertex_colors', False)

        out = nodes.new('ShaderNodeOutputMaterial')
        out.location = (700, 0)

        if use_vc_blend:
            # Vertex colors carry pre-baked scene lighting, so skip PBR and
            # use Emission so the material ignores Blender scene lights.
            shader = nodes.new('ShaderNodeEmission')
            shader.location = (450, 0)
            shader.inputs['Strength'].default_value = 1.0
            # Output link is deferred: may need Mix Shader for transparency.
            color_input = shader.inputs['Color']
        else:
            shader = nodes.new('ShaderNodeBsdfPrincipled')
            shader.location = (450, 0)
            links.new(shader.outputs['BSDF'], out.inputs['Surface'])
            shader.inputs['Base Color'].default_value = md['diffuse']
            color_input = shader.inputs['Base Color']

        ti = md['texIndex']
        tex_has_alpha = False
        tex_is_solid  = False
        alpha_socket  = None

        if 0 <= ti < len(bl_images):
            tex_has_alpha = bl_images_has_alpha[ti]
            tex_is_solid  = bl_images_is_solid[ti]
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.location = (-400, 0)
            tex_node.image = bl_images[ti]
            alpha_socket = tex_node.outputs['Alpha']

            # PSO uses mirror-repeat wrapping on all stage textures.
            # Replicate with Ping Pong nodes on each UV axis.
            uv_node = nodes.new('ShaderNodeUVMap')
            uv_node.uv_map = "UVMap"
            uv_node.location = (-1200, 0)

            sep = nodes.new('ShaderNodeSeparateXYZ')
            sep.location = (-1000, 0)
            links.new(uv_node.outputs['UV'], sep.inputs['Vector'])

            pp_u = nodes.new('ShaderNodeMath')
            pp_u.operation = 'PINGPONG'
            pp_u.inputs[1].default_value = 1.0
            pp_u.location = (-800, 70)
            links.new(sep.outputs['X'], pp_u.inputs[0])

            pp_v = nodes.new('ShaderNodeMath')
            pp_v.operation = 'PINGPONG'
            pp_v.inputs[1].default_value = 1.0
            pp_v.location = (-800, -90)
            links.new(sep.outputs['Y'], pp_v.inputs[0])

            comb = nodes.new('ShaderNodeCombineXYZ')
            comb.location = (-600, 0)
            links.new(pp_u.outputs['Value'], comb.inputs['X'])
            links.new(pp_v.outputs['Value'], comb.inputs['Y'])
            links.new(comb.outputs['Vector'], tex_node.inputs['Vector'])

            if tex_has_alpha:
                gt_node = nodes.new('ShaderNodeMath')
                gt_node.operation = 'GREATER_THAN'
                gt_node.inputs[1].default_value = 0.5
                gt_node.location = (-150, -200)
                links.new(tex_node.outputs['Alpha'], gt_node.inputs[0])
                alpha_socket = gt_node.outputs['Value']

            if use_vc_blend:
                # Exact blend used by the original game:
                #   step 1 — vc_squared = Multiply(Col, Col)
                #   step 2 — final      = Multiply(texture, vc_squared)
                col_attr = nodes.new('ShaderNodeAttribute')
                col_attr.attribute_name = "Col"
                col_attr.location = (-400, -280)

                vc_self_mul = nodes.new('ShaderNodeMixRGB')
                vc_self_mul.blend_type = 'MULTIPLY'
                vc_self_mul.inputs['Fac'].default_value = 1.0
                vc_self_mul.location = (-100, -200)
                links.new(col_attr.outputs['Color'], vc_self_mul.inputs['Color1'])
                links.new(col_attr.outputs['Color'], vc_self_mul.inputs['Color2'])

                tex_vc_mul = nodes.new('ShaderNodeMixRGB')
                tex_vc_mul.blend_type = 'MULTIPLY'
                tex_vc_mul.inputs['Fac'].default_value = 1.0
                tex_vc_mul.location = (150, 0)
                links.new(tex_node.outputs['Color'],    tex_vc_mul.inputs['Color1'])
                links.new(vc_self_mul.outputs['Color'], tex_vc_mul.inputs['Color2'])

                links.new(tex_vc_mul.outputs['Color'], color_input)
            else:
                links.new(tex_node.outputs['Color'], color_input)

            if not use_vc_blend:
                if tex_is_solid:
                    # Solid-color texture: fixed 50% alpha, no socket connection
                    shader.inputs['Alpha'].default_value = 0.5
                else:
                    links.new(alpha_socket, shader.inputs['Alpha'])

            if tex_has_alpha:
                mat.surface_render_method = 'DITHERED'
            elif md['diffuse'][3] < 1.0 or md['blendSrc'] or tex_is_solid:
                mat.surface_render_method = 'BLENDED'

        # --- Final surface → output wiring, with transparency for Emission ---
        if use_vc_blend:
            if tex_has_alpha and alpha_socket is not None:
                # Texture alpha drives mix: alpha=0 → Transparent, alpha=1 → Emission
                out.location = (950, 0)
                transp = nodes.new('ShaderNodeBsdfTransparent')
                transp.location = (450, -180)
                mix = nodes.new('ShaderNodeMixShader')
                mix.location = (700, 0)
                links.new(alpha_socket,                mix.inputs['Fac'])
                links.new(transp.outputs['BSDF'],      mix.inputs[1])
                links.new(shader.outputs['Emission'],  mix.inputs[2])
                links.new(mix.outputs['Shader'],       out.inputs['Surface'])
                mat.surface_render_method = 'DITHERED'
            elif tex_is_solid:
                # Solid-color texture: fixed 50% mix, no socket connection
                out.location = (950, 0)
                transp = nodes.new('ShaderNodeBsdfTransparent')
                transp.location = (450, -180)
                mix = nodes.new('ShaderNodeMixShader')
                mix.location = (700, 0)
                mix.inputs['Fac'].default_value = 0.5
                links.new(transp.outputs['BSDF'],      mix.inputs[1])
                links.new(shader.outputs['Emission'],  mix.inputs[2])
                links.new(mix.outputs['Shader'],       out.inputs['Surface'])
                mat.surface_render_method = 'BLENDED'
            else:
                links.new(shader.outputs['Emission'], out.inputs['Surface'])

        mat.use_backface_culling = not md.get('doubleSided', False)
        bl_materials.append(mat)

    # --- Collection for this import ---
    col_name   = os.path.splitext(os.path.basename(filepath))[0]
    collection = bpy.data.collections.new(col_name)
    bpy.context.scene.collection.children.link(collection)

    # --- Meshes -> Blender mesh objects ---
    name_counters = {}   # base_name -> next integer suffix

    for i, md in enumerate(geo.meshes_data):
        verts = md['positions']
        tris  = md['triangles']    # sequential: [0,1,2,3,4,5,...]
        faces = [[tris[j], tris[j+1], tris[j+2]] for j in range(0, len(tris), 3)]

        # Derive a meaningful base name from the mesh's texture
        base_name = "mesh"
        mat_idx = md.get('mat_index', -1)
        if 0 <= mat_idx < len(geo.materials_data):
            ti = geo.materials_data[mat_idx].get('texIndex', -1)
            if 0 <= ti < len(geo.textures):
                base_name = geo.textures[ti]['name']

        count = name_counters.get(base_name, 0)
        name_counters[base_name] = count + 1
        obj_name = "%s_%03d" % (base_name, count)

        bl_mesh = bpy.data.meshes.new(obj_name)
        bl_mesh.from_pydata(verts, [], faces)

        # UV coordinates
        if md['uvs'] and len(md['uvs']) == len(verts):
            uv_layer = bl_mesh.uv_layers.new(name="UVMap")
            for loop in bl_mesh.loops:
                uv_layer.data[loop.index].uv = md['uvs'][loop.vertex_index]

        # Custom split normals (per-loop, matching expanded vertex list)
        if md['normals'] and len(md['normals']) == len(verts):
            bl_mesh.normals_split_custom_set(
                [md['normals'][loop.vertex_index] for loop in bl_mesh.loops]
            )

        # Vertex colors (Blender 3.2+ color attributes; falls back for older)
        if md['colors'] and len(md['colors']) == len(verts):
            try:
                col_attr = bl_mesh.color_attributes.new(
                    name="Col", type='FLOAT_COLOR', domain='POINT'
                )
                for vi, c in enumerate(md['colors']):
                    col_attr.data[vi].color = c
            except Exception:
                try:
                    vcol = bl_mesh.vertex_colors.new(name="Col")
                    for loop in bl_mesh.loops:
                        vcol.data[loop.index].color = md['colors'][loop.vertex_index]
                except Exception:
                    pass

        bl_mesh.update()

        # Assign material
        if md['mat_index'] < len(bl_materials):
            bl_mesh.materials.append(bl_materials[md['mat_index']])

        obj = bpy.data.objects.new(obj_name, bl_mesh)
        obj.rotation_euler = (math.radians(90), 0, 0)
        collection.objects.link(obj)

    return len(geo.meshes_data)

# ============================================================
# Ninja XJ importer (props / character models)
# Produces the same meshes_data / materials_data / textures
# structure as NinjaStageGeometry so build_blender_scene reuses it.
# ============================================================
class NinjaXJImporter:

    def __init__(self):
        self.texNames       = []   # filename strings from NJTL chunk
        self.vertex_stack   = {}
        self.materials_data = []
        self.meshes_data    = []
        self.textures       = []   # from .xvm
        self.current_matrix = DashMat4()
        self.material       = {}

    def setTextures(self, textures):
        self.textures = textures

    # ------------------------------------------------------------------
    def parse(self, data):
        outer = BitStream(data)
        size  = outer.getSize() - 4

        while outer.tell() < size:
            magic = outer.readUInt()

            if magic == MAGIC_NJTL:
                chunk_len = outer.readUInt()
                buf       = outer.readBytes(chunk_len)
                self.bs   = BitStream(buf)
                self.readList()
                # Apply the human-readable filenames from NJTL to the
                # already-loaded texture dicts so images get proper names.
                for idx, raw_name in enumerate(self.texNames):
                    if idx < len(self.textures):
                        clean = os.path.splitext(os.path.basename(raw_name))[0]
                        if clean:
                            self.textures[idx]['name'] = clean

            elif magic == MAGIC_NJCM:
                chunk_len = outer.readUInt()
                buf       = outer.readBytes(chunk_len)
                self.bs   = BitStream(buf)
                self.readBone()

            elif magic == MAGIC_NMDM:
                # Animation data – skip for now
                chunk_len = outer.readUInt()
                outer.readBytes(chunk_len)

            else:
                # Unknown chunk — try to skip safely by reading the length
                try:
                    chunk_len = outer.readUInt()
                    outer.readBytes(chunk_len)
                except Exception:
                    break

    # ------------------------------------------------------------------
    def readList(self):
        """Read NJTL texture-name list chunk."""
        list_ofs      = self.bs.readUInt()
        texture_count = self.bs.readUInt()
        self.bs.seek(list_ofs)

        str_offsets = []
        for _ in range(texture_count):
            ofs = self.bs.readUInt()
            str_offsets.append(ofs)
            self.bs.seek(8, 1)   # skip 8 unknown bytes (NOESEEK_REL)

        for ofs in str_offsets:
            self.bs.seek(ofs)
            self.texNames.append(self.bs.readString())

    # ------------------------------------------------------------------
    def readBone(self, pMatrix=None):
        if self.bs.pos + 52 > self.bs.getSize():
            return
        c    = 2.0 * math.pi / 0x10000
        node = {
            'flags':      self.bs.readUInt(),
            'meshOfs':    self.bs.readUInt(),
            'pos':        (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
            'rot':        (self.bs.readInt()*c, self.bs.readInt()*c, self.bs.readInt()*c),
            'scl':        (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
            'childOfs':   self.bs.readUInt(),
            'siblingOfs': self.bs.readUInt(),
        }

        mat = DashMat4()
        if not (node['flags'] & 0x02):
            mat.rotate(node['rot'])
        if not (node['flags'] & 0x01):
            mat.translate(node['pos'])
        if pMatrix is not None:
            mat.compose(pMatrix)

        self.current_matrix = mat

        size = self.bs.getSize()
        if node['meshOfs'] >= size or node['childOfs'] >= size or node['siblingOfs'] >= size:
            return

        if node['meshOfs'] != 0:
            self.bs.seek(node['meshOfs'])
            self.readMesh()

        if node['childOfs'] != 0:
            self.bs.seek(node['childOfs'])
            self.readBone(mat)

        if node['siblingOfs'] != 0:
            self.bs.seek(node['siblingOfs'])
            self.readBone(pMatrix)

    # ------------------------------------------------------------------
    def readMesh(self):
        mesh = {
            'flags':                        self.bs.readUInt(),
            'vertex_info_list_offset':      self.bs.readUInt(),
            'vertex_info_count':            self.bs.readUInt(),
            'triangle_strip_list_a_offset': self.bs.readUInt(),
            'triangle_strip_a_count':       self.bs.readUInt(),
            'triangle_strip_list_b_offset': self.bs.readUInt(),
            'triangle_strip_b_count':       self.bs.readUInt(),
            'center': (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
            'radius': self.bs.readFloat(),
        }

        sz = self.bs.getSize()
        vlo = mesh['vertex_info_list_offset']
        if vlo and vlo < sz:
            self.bs.seek(vlo)
            self.readVertexList()

        if mesh['triangle_strip_a_count']:
            aso = mesh['triangle_strip_list_a_offset']
            if aso and aso < sz:
                self.bs.seek(aso)
                self.readStripList(mesh['triangle_strip_a_count'], False)

        if mesh['triangle_strip_b_count']:
            bso = mesh['triangle_strip_list_b_offset']
            if bso and bso < sz:
                self.bs.seek(bso)
                self.readStripList(mesh['triangle_strip_b_count'], True)

    # ------------------------------------------------------------------
    def readVertexList(self):
        vtype  = self.bs.readUShort()
        self.bs.readUShort()        # unknown
        vofs   = self.bs.readUInt()
        self.bs.readUInt()          # vertex_size
        vcount = self.bs.readUInt()

        read_uv     = bool(vtype & 0x01)
        read_normal = bool(vtype & 0x02)
        read_color  = bool(vtype & 0x04)

        sz = self.bs.getSize()
        if not vofs or vofs >= sz:
            return
        self.bs.seek(vofs)
        self.vertex_stack = {}

        for i in range(vcount):
            vertex = {'pos': None, 'norm': None, 'color': None, 'uv': None}

            p = (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat())
            vertex['pos'] = self.current_matrix.transformPoint(p)

            if read_normal:
                n = (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat())
                vertex['norm'] = self.current_matrix.transformNormal(n)

            if read_color:
                # XJ vertex colors are stored in RGBA order
                r = self.bs.readUByte() / 255.0
                g = self.bs.readUByte() / 255.0
                b = self.bs.readUByte() / 255.0
                a = self.bs.readUByte() / 255.0
                vertex['color'] = (r, g, b, a)

            if read_uv:
                u = self.bs.readFloat()
                v = self.bs.readFloat()
                vertex['uv'] = (u, 1.0 - v)   # flip V for Blender

            self.vertex_stack[i] = vertex

    # ------------------------------------------------------------------
    def readStripList(self, count, useAlpha):
        strip_info = []
        for _ in range(count):
            strip_info.append({
                'material_property_list_offset': self.bs.readUInt(),
                'material_property_list_size':   self.bs.readUInt(),
                'index_list_offset':             self.bs.readUInt(),
                'index_count':                   self.bs.readUInt(),
                'unknown':                       self.bs.readUInt(),
            })

        self.material = {
            'diffuse':    (1.0, 1.0, 1.0, 1.0),
            'ambient':    (1.0, 1.0, 1.0, 1.0),
            'specular':   (1.0, 1.0, 1.0, 1.0),
            'texIndex':   -1,
            'blendSrc':   '',
            'blendDst':   '',
            'doubleSided': False,
        }

        for strip in strip_info:
            self.bs.seek(strip['material_property_list_offset'])
            self.readMaterial(strip['material_property_list_size'])

            points = []
            self.bs.seek(strip['index_list_offset'])
            for _ in range(strip['index_count']):
                points.append(self.bs.readShort())

            # XJ strips: every non-degenerate consecutive triple becomes
            # two triangles (both windings) for double-sided geometry.
            triangles = []
            for i in range(len(points) - 2):
                a, b, c = points[i], points[i + 1], points[i + 2]
                if a == b or b == c or c == a:
                    continue
                triangles.extend([a, b, c])
                triangles.extend([a, c, b])

            self.appendMesh(triangles)

    # ------------------------------------------------------------------
    def readMaterial(self, prop_count):
        for _ in range(prop_count):
            mat_type = self.bs.readUInt()
            if mat_type == 2:
                dst = self.bs.readUInt()
                src = self.bs.readUInt()
                if src == 1 and dst == 4:
                    self.material['blendSrc'] = 'ONE'
                    self.material['blendDst'] = 'ONE'
                elif src == 5 and dst == 4:
                    self.material['blendSrc'] = ''
                    self.material['blendDst'] = ''
                self.bs.readBytes(4)
            elif mat_type == 3:
                tex_id = self.bs.readUInt()
                self.bs.readBytes(8)
                self.material['texIndex'] = tex_id
            elif mat_type == 5:
                r = self.bs.readUByte() / 255.0
                g = self.bs.readUByte() / 255.0
                b = self.bs.readUByte() / 255.0
                a = self.bs.readUByte() / 255.0
                self.material['diffuse'] = (r, g, b, a)
                self.bs.readBytes(8)
            else:
                self.bs.readBytes(12)

    # ------------------------------------------------------------------
    def appendMesh(self, triangles):
        if not triangles:
            return

        pos_list   = []
        norm_list  = []
        color_list = []
        uv_list    = []
        tri_list   = []

        for point in triangles:
            if point not in self.vertex_stack:
                continue
            vert = self.vertex_stack[point]
            tri_list.append(len(pos_list))
            pos_list.append(vert['pos'])
            if vert['norm']  is not None: norm_list.append(vert['norm'])
            if vert['color'] is not None: color_list.append(vert['color'])
            if vert['uv']    is not None: uv_list.append(vert['uv'])

        if not pos_list:
            return

        has_colors = bool(color_list)
        mat_key    = (
            self.material['diffuse'],
            self.material['texIndex'],
            self.material['blendSrc'],
            self.material['blendDst'],
            has_colors,
        )
        mat_index = next(
            (i for i, m in enumerate(self.materials_data) if m['key'] == mat_key),
            None
        )
        if mat_index is None:
            mat_index = len(self.materials_data)
            self.materials_data.append({
                'key':               mat_key,
                'name':              "mat_%03d" % mat_index,
                'diffuse':           self.material['diffuse'],
                'texIndex':          self.material['texIndex'],
                'blendSrc':          self.material['blendSrc'],
                'blendDst':          self.material['blendDst'],
                'doubleSided':       self.material['doubleSided'],
                'has_vertex_colors': has_colors,
            })

        self.meshes_data.append({
            'positions': pos_list,
            'normals':   norm_list,
            'colors':    color_list,
            'uvs':       uv_list,
            'triangles': tri_list,
            'mat_index': mat_index,
        })


# ============================================================
# NJD Chunk Mixin — shared mesh reading for DC .nj and DC .rel
# ============================================================
class NinjaChunkMixin:
    """
    Subclasses must expose: current_matrix (DashMat4), vertex_stack (dict),
    material (dict), materials_data (list), meshes_data (list),
    textures (list), texNames (list), store_ofs (list[10]), jump_to (int).
    """

    def readChunks(self, bs):
        self.material = {
            'diffuse': (1.0,1.0,1.0,1.0), 'ambient': (1.0,1.0,1.0,1.0),
            'specular': (1.0,1.0,1.0,1.0), 'texIndex': -1,
            'blendSrc': '', 'blendDst': '', 'doubleSided': False,
        }
        self._do_read = True
        gc = bs._e == '>'
        while self._do_read:
            if bs.pos + 2 > bs.getSize():
                break   # not enough bytes left for any chunk header
            if gc:
                # GC big-endian NJ: chunk header words are stored BE.
                # "No-length" chunk types (NJD_CN/CE, BITS 0x02-0x03, TINY 0x10-0x1F):
                #   first BE uint16 = ch_cf word  (ch in low byte, cf in high byte)
                # "With-length" chunk types (VERTEX only in practice):
                #   first BE uint16  = length word (discarded by handlers)
                #   second BE uint16 = ch_cf word  (ch in low byte, cf in high byte)
                # All other types (TINY, MATERIAL, STRIP, VOLUME) put ch_cf first,
                # then a length word (handled by the individual chunk methods).
                word0 = bs.readUShort()
                ch_cand = word0 & 0xFF
                no_len = (ch_cand == 0 or ch_cand == 0xFF or
                          ch_cand in CHUNK_BITS or
                          ch_cand in CHUNK_TINY or
                          ch_cand in CHUNK_STRIP or
                          ch_cand in CHUNK_VOLUME or
                          0x10 <= ch_cand <= 0x1F)
                if no_len:
                    ch = ch_cand
                    cf = (word0 >> 8) & 0xFF
                else:
                    # word0 was the length; read the actual ch_cf word
                    word1 = bs.readUShort()
                    ch = word1 & 0xFF
                    cf = (word1 >> 8) & 0xFF
            else:
                ch = bs.readUByte()
                cf = bs.readUByte()
            if   ch == NJD_CE:
                if self.jump_to:
                    bs.seek(self.jump_to); self.jump_to = 0; continue
                self._do_read = False
            elif ch == NJD_CN:           continue
            elif ch in CHUNK_VERTEX:
                try:    self._vChunk(bs, ch, cf)
                except struct.error: break
            elif ch in CHUNK_BITS:       self._bChunk(bs, ch, cf)
            elif ch in CHUNK_MATERIAL:
                try:    self._mChunk(bs, ch, cf)
                except struct.error: break
            elif ch in CHUNK_TINY:
                try:    self._tChunk(bs, ch, cf)
                except struct.error: break
            elif ch in CHUNK_STRIP:
                try:    self._sChunk(bs, ch, cf)
                except struct.error: break
            elif ch in CHUNK_VOLUME:
                try:    self._volChunk(bs, ch, cf)
                except struct.error: pass
                return

    def _vChunk(self, bs, ch, cf):
        if bs._e == '>':
            # GC: length was consumed in readChunks; vcount precedes vofs
            vcount = bs.readUShort()
            vofs   = bs.readUShort()
        else:
            bs.readUShort()                # chunk length (words)
            vofs   = bs.readUShort()       # index offset into stack
            vcount = bs.readUShort()

        read_color  = ch in (NJD_CV_VN_D8, NJD_CV_VNX_D8, NJD_CV_D8)
        read_normal = (NJD_CV_VN <= ch <= NJD_CV_VNX_UF)
        is_sh       = ch in (NJD_CV_SH, NJD_CV_VN_SH)
        is_vnx      = ch in (NJD_CV_VNX, NJD_CV_VNX_D8, NJD_CV_VNX_UF)

        for i in range(vcount):
            v = {'pos': None, 'norm': None, 'color': None}

            p = (bs.readFloat(), bs.readFloat(), bs.readFloat())
            if is_sh: bs.readFloat()       # SH4 w=1.0 padding
            v['pos'] = self.current_matrix.transformPoint(p)

            if is_vnx:
                pk = bs.readUInt()
                nx = (((pk >> 20) & 0x3FF) / 511.0) - 1.0
                ny = (((pk >> 10) & 0x3FF) / 511.0) - 1.0
                nz = (( pk        & 0x3FF) / 511.0) - 1.0
                v['norm'] = self.current_matrix.transformNormal((nx, ny, nz))
            elif read_normal:
                n = (bs.readFloat(), bs.readFloat(), bs.readFloat())
                if is_sh: bs.readFloat()   # SH4 w=0.0 padding
                v['norm'] = self.current_matrix.transformNormal(n)

            if read_color:
                b2 = bs.readUByte()/255.0; g2 = bs.readUByte()/255.0
                r2 = bs.readUByte()/255.0; a2 = bs.readUByte()/255.0
                v['color'] = (r2, g2, b2, a2)

            if ch == NJD_CV_VN_NF:
                nofs = bs.readShort(); bs.readShort()
                key  = str(vofs + nofs)
                # Use the stream position (already transformed to world space by the
                # current bone's matrix). Borrowing the world-space position from a
                # sibling bone's stack entry would place the vertex on the wrong side
                # of the body for mirrored limbs (e.g. PSO BB character arms).
                self.vertex_stack[key] = v
            else:
                self.vertex_stack[str(vofs + i)] = v

    def _bChunk(self, bs, ch, cf):
        if ch == NJD_CB_CP:
            self._do_read = False
            self.store_ofs[cf] = bs.tell()
        elif ch == NJD_CB_DP:
            self.jump_to = bs.tell()
            bs.seek(self.store_ofs[cf])

    def _mChunk(self, bs, ch, cf):
        bs.readUShort()   # chunk_len (words); present after ch_cf for both LE and GC
        src = cf & 0x07; dst = (cf >> 3) & 0x07
        if   src == 1 and dst == 4: self.material['blendSrc'] = 'ONE'; self.material['blendDst'] = 'ONE'
        elif src == 5 and dst == 4: self.material['blendSrc'] = '';    self.material['blendDst'] = ''
        if ch & 0x01:               # diffuse BGRA
            b2,g2,r2,a2 = bs.readUByte()/255.0, bs.readUByte()/255.0, bs.readUByte()/255.0, bs.readUByte()/255.0
            self.material['diffuse'] = (r2, g2, b2, a2)
        if ch & 0x02:               # ambient BGRN
            b2,g2,r2 = bs.readUByte()/255.0, bs.readUByte()/255.0, bs.readUByte()/255.0; bs.readUByte()
            self.material['ambient'] = (r2, g2, b2, 1.0)
        if ch & 0x04:               # specular BGRE
            b2,g2,r2 = bs.readUByte()/255.0, bs.readUByte()/255.0, bs.readUByte()/255.0; bs.readUByte()
            self.material['specular'] = (r2, g2, b2, 1.0)

    def _tChunk(self, bs, ch, cf):
        body   = bs.readUShort()
        tex_id = body & 0x1FFF
        self.material['texIndex'] = tex_id   # validated at mesh-build time

    def _volChunk(self, bs, ch, cf):
        bs.readUShort()   # chunk_len; present after ch_cf for both LE and GC
        body        = bs.readUShort()
        strip_count = body & 0x3FFF
        triangles   = []
        for _ in range(strip_count):
            raw   = bs.readShort()
            cw    = raw < 0
            slen  = abs(raw)
            strip = [{'index': str(bs.readUShort()), 'uv': None} for _ in range(slen)]
            for k in range(slen - 2):
                if cw and k%2==0:     a,b,c = strip[k],strip[k+2],strip[k+1]
                elif cw:              a,b,c = strip[k+1],strip[k+2],strip[k]
                elif k%2==0:          a,b,c = strip[k],strip[k+1],strip[k+2]
                else:                 a,b,c = strip[k],strip[k+2],strip[k+1]
                triangles.extend([a, b, c])
        self._appendPoints(triangles)

    def _sChunk(self, bs, ch, cf):
        bs.readUShort()   # chunk_len; present after ch_cf for both LE and GC
        body        = bs.readUShort()
        double_side = cf & 0x10
        strip_count = body & 0x3FFF
        user_offset = body >> 14
        triangles   = []

        for _ in range(strip_count):
            raw  = bs.readShort()
            cw   = raw < 0
            slen = abs(raw)
            strip = []
            for k in range(slen):
                pt = {'index': str(bs.readUShort()), 'uv': None}
                if ch == NJD_CS_UVN:
                    u = bs.readShort()/255.0;  v = bs.readShort()/255.0
                    pt['uv'] = (u, 1.0-v)
                elif ch == NJD_CS_UVH:
                    u = bs.readShort()/1023.0; v = bs.readShort()/1023.0
                    pt['uv'] = (u, 1.0-v)
                strip.append(pt)
                if k > 1 and user_offset:
                    bs.readBytes(user_offset * 2)

            for k in range(slen - 2):
                if cw:   a,b,c = strip[k],strip[k+2],strip[k+1]
                else:    a,b,c = strip[k+1],strip[k+2],strip[k]
                cw = not cw
                triangles.extend([a, b, c])
                if double_side: triangles.extend([b, a, c])

        self._appendPoints(triangles)

    def _appendPoints(self, triangles):
        if not triangles: return
        pos_list=[]; norm_list=[]; color_list=[]; uv_list=[]; tri_list=[]
        # Process complete triangles atomically: skip any triangle where a vertex
        # is absent from the stack (avoids a non-multiple-of-3 triangle list).
        for j in range(0, len(triangles) - 2, 3):
            pts = (triangles[j], triangles[j+1], triangles[j+2])
            if any(pt['index'] not in self.vertex_stack for pt in pts):
                continue
            for pt in pts:
                vt = self.vertex_stack[pt['index']]
                tri_list.append(len(pos_list))
                pos_list.append(vt['pos'])
                if vt.get('norm'):  norm_list.append(vt['norm'])
                if vt.get('color'): color_list.append(vt['color'])
                if pt.get('uv'):    uv_list.append(pt['uv'])
        if not pos_list: return
        has_vc  = bool(color_list)
        mat_key = (self.material['diffuse'], self.material['texIndex'],
                   self.material['blendSrc'], self.material['blendDst'], has_vc)
        mi = next((i for i,m in enumerate(self.materials_data) if m['key']==mat_key), None)
        if mi is None:
            mi = len(self.materials_data)
            self.materials_data.append({
                'key': mat_key, 'name': "mat_%03d" % mi,
                'diffuse': self.material['diffuse'], 'texIndex': self.material['texIndex'],
                'blendSrc': self.material['blendSrc'], 'blendDst': self.material['blendDst'],
                'doubleSided': False, 'has_vertex_colors': has_vc,
            })
        self.meshes_data.append({
            'positions': pos_list, 'normals': norm_list, 'colors': color_list,
            'uvs': uv_list, 'triangles': tri_list, 'mat_index': mi,
        })


# ============================================================
# POF0 relocation helpers
# ============================================================
def parse_pof0(payload):
    """
    Decode a POF0 relocation payload into a list of byte offsets.
    Each offset marks a location within the NJCM payload that holds a
    pointer value needing the serialization base subtracted.

    Encoding: variable-length difference-coded.  Each byte's top 2 bits
    choose the jump width:
      0x01-0x3F  small  — advance by (byte * 4)
      0x40-0x7F  medium — advance by ((byte & 0x3F) << 8  | next) * 4
      0x80-0xBF  large  — advance by ((byte & 0x3F) << 16 | next2) * 4
      0x00       end of list
    """
    offsets = []
    pos = 0
    current = 0
    n = len(payload)
    while pos < n:
        b = payload[pos]; pos += 1
        if b == 0:
            break
        top2 = b >> 6
        if top2 == 0:
            delta = b * 4
        elif top2 == 1:
            if pos >= n:
                break
            b2 = payload[pos]; pos += 1
            delta = ((b & 0x3F) << 8 | b2) * 4
        elif top2 == 2:
            if pos + 1 >= n:
                break
            b2 = payload[pos]; b3 = payload[pos + 1]; pos += 2
            delta = ((b & 0x3F) << 16 | b2 << 8 | b3) * 4
        else:
            break  # 0xC0+ not used in PSO
        current += delta
        offsets.append(current)
    return offsets


def apply_pof0_relocation(njcm_payload, pof0_payload, big_endian=False):
    """
    Patch an NJCM payload using a POF0 relocation table.

    Reads the pointer values at every offset listed in the POF0 table.
    If any pointer exceeds the payload size, a serialization base B is
    inferred and subtracted from every non-zero pointer in the table.

    Base detection: iterate over candidate "true minimum offsets" (the
    target of the smallest non-null pointer in the file, typically 52 for
    the first child bone) to derive a candidate B = min_raw_ptr - true_min.
    Each candidate is validated by checking that:
      (a) all non-zero adjusted pointers stay inside the payload, and
      (b) the adjusted targets of the childOfs/siblingOfs fields at known
          bone-node offsets (44, 48, 96, 100, …) look like valid NJ bone
          flags (<= 0x3FFF).

    Returns a new bytes object (the patched payload), or the original
    payload unchanged if no relocation is necessary or no valid B found.
    """
    ptr_offsets = parse_pof0(pof0_payload)
    if not ptr_offsets:
        return njcm_payload

    payload_size = len(njcm_payload)
    bo = '>' if big_endian else '<'

    # Collect (file_offset, raw_value) for every listed pointer
    raw_ptrs = []
    for off in ptr_offsets:
        if off + 4 <= payload_size:
            v, = struct.unpack_from(bo + 'I', njcm_payload, off)
            raw_ptrs.append((off, v))

    if not raw_ptrs:
        return njcm_payload

    non_zero_vals = [v for _, v in raw_ptrs if v != 0]
    if not non_zero_vals or max(non_zero_vals) < payload_size:
        return njcm_payload  # all pointers already valid

    # Some files mix already-valid pointers (< payload_size) with clearly
    # invalid ones (≥ payload_size).  Compute B only from the invalid
    # ones — they supply the lower bound on B.  After applying B the
    # valid-looking pointers (which also have base embedded) just become
    # smaller and remain in-range.
    invalid_vals = [v for v in non_zero_vals if v >= payload_size]
    valid_vals   = [v for v in non_zero_vals if v <  payload_size]

    if not invalid_vals:
        return njcm_payload  # nothing to relocate

    min_inv = min(invalid_vals)
    max_inv = max(invalid_vals)

    # B must:
    #   (a) bring every invalid pointer in-range:  max_inv - B < payload_size
    #                                           →  B > max_inv - payload_size
    #   (b) keep every invalid pointer positive:   min_inv - B ≥ 4
    #                                           →  B ≤ min_inv - 4
    #   (c) keep every valid-but-relocated ptr positive:
    #                                              B < min(valid_vals) if any
    b_lo = (max_inv - payload_size + 4) & ~3
    b_hi = (min_inv - 4) & ~3
    if valid_vals:
        b_hi = min(b_hi, (min(valid_vals) - 4) & ~3)

    if b_lo > b_hi:
        # Invalid pointer values span a range larger than the payload; the
        # POF0 for this chunk likely uses absolute file offsets rather than
        # NJCM-relative offsets.  Skip relocation — the parser will fall back
        # to whatever valid pointers already exist in the payload.
        print("[PSO POF0] Skipping relocation: invalid pointer range "
              "(0x%X..0x%X) exceeds payload size %d — "
              "probable absolute-offset POF0" % (min_inv, max_inv, payload_size))
        return njcm_payload

    def looks_like_bone(offset):
        """Return True if bytes at offset could be the start of a NJ bone node."""
        if offset + 4 > payload_size:
            return False
        flags, = struct.unpack_from(bo + 'I', njcm_payload, offset)
        return flags <= 0x3FFF

    # The root bone is always at NJCM offset 0 (NJ spec).
    # Its childOfs field is at offset 44 and siblingOfs at offset 48.
    # These are always bone pointers (never mesh pointers), so their targets
    # reliably start with an NJ flags word.  Use them as anchor samples for
    # the structure check; fall back to the first 5 non-zero values if
    # neither root-bone field appears in the relocation table.
    ptr_off_map = {off: v for off, v in raw_ptrs if v != 0}
    bone_ptr_vals = [ptr_off_map[k] for k in (44, 48) if k in ptr_off_map]
    # Only sample invalid-range values; valid-range values could be mesh
    # pointers whose targets don't start with a bone-flags word.
    invalid_bone_samples = [v for v in bone_ptr_vals if v >= payload_size]
    sample_vals = (invalid_bone_samples if invalid_bone_samples
                   else [v for v in invalid_vals[:5]])

    # Iterate candidate true-offsets for the minimum *invalid* pointer
    # (min_inv = min_true_offset + B → B = min_inv - true_min).
    # At most 1024 iterations of O(N) constraint checks — very fast.
    best_B = None
    for true_min in range(4, min(4097, payload_size), 4):
        B = min_inv - true_min
        if B < b_lo or B > b_hi:
            continue
        # All invalid pointers must land inside the payload after adjustment
        if not all(0 < v - B < payload_size for v in invalid_vals):
            continue
        # Valid pointers must still be positive after adjustment
        if valid_vals and not all(v - B > 0 for v in valid_vals):
            continue
        if all(looks_like_bone(v - B) for v in sample_vals):
            best_B = B
            break

    if best_B is None:
        # Relax structure check; use the largest B satisfying the constraints.
        for true_min in range(4, payload_size, 4):
            B = min_inv - true_min
            if not (b_lo <= B <= b_hi):
                continue
            if not all(0 < v - B < payload_size for v in invalid_vals):
                continue
            if valid_vals and not all(v - B > 0 for v in valid_vals):
                continue
            best_B = B
            break

    if best_B is None:
        print("[PSO POF0] No valid relocation base found in range [%d, %d]" % (b_lo, b_hi))
        return njcm_payload

    print("[PSO POF0] Applying relocation base 0x%X to %d pointer(s) "
          "(%d invalid, %d valid-but-relocated)" % (
          best_B, len(raw_ptrs), len(invalid_vals), len(valid_vals)))

    data = bytearray(njcm_payload)
    for off, v in raw_ptrs:
        if v == 0:
            continue
        adjusted = v - best_B
        if 0 < adjusted < payload_size:
            struct.pack_into(bo + 'I', data, off, adjusted)
        else:
            print("[PSO POF0] Warning: pointer 0x%X at 0x%X → "
                  "adjusted 0x%X still invalid" % (v, off, adjusted))
    return bytes(data)


# ============================================================
# DC .nj model importer
# ============================================================
class NinjaDCImporter(NinjaChunkMixin):

    def __init__(self):
        self.texNames       = []
        self.vertex_stack   = {}
        self.materials_data = []
        self.meshes_data    = []
        self.textures       = []
        self.current_matrix = DashMat4()
        self.material       = {}
        self.store_ofs      = [None] * 256
        self.jump_to        = 0

    def setTextures(self, textures): self.textures = textures

    def parse(self, data):
        # ── Pass 1: collect chunk locations without consuming data ──────────
        chunk_map = {}   # magic → [(payload_offset, payload_length), ...]
        tmp  = BitStream(data)
        size = tmp.getSize() - 4
        while tmp.tell() < size:
            magic = tmp.readUInt()
            if magic in (MAGIC_NJTL, MAGIC_NJCM, MAGIC_NMDM, MAGIC_POF0):
                clen = tmp.readUInt()
                chunk_map.setdefault(magic, []).append((tmp.tell(), clen))
                tmp.seek(clen, 1)
            else:
                try: tmp.seek(tmp.readUInt(), 1)
                except: break

        # ── Auto-detect endianness from first NJCM root node ───────────────
        # The GC version of PSO stores NJ chunk *payloads* in big-endian byte
        # order even though the outer chunk headers (magic + length) remain
        # little-endian.  We check whether flags and meshOfs look sane under
        # each interpretation; scale=1.0 (0x3f800000 BE) is the tiebreaker.
        big_endian = False
        if MAGIC_NJCM in chunk_map:
            off, clen = chunk_map[MAGIC_NJCM][0]
            if off + 48 <= len(data):
                for be in (False, True):
                    bo  = '>' if be else '<'
                    fl  = struct.unpack_from(bo+'I', data, off)[0]
                    mo  = struct.unpack_from(bo+'I', data, off+4)[0]
                    sx  = struct.unpack_from(bo+'f', data, off+32)[0]
                    # Accept if flags are small, meshOfs is in-bounds (or 0),
                    # and scale.x is near 1.0 (strong indicator of correct endian)
                    if fl <= 0x1FFF and mo <= clen and 0.1 <= abs(sx) <= 10.0:
                        big_endian = be
                        break

        # ── Pass 2: parse NJTL then NJCM with the detected byte order ──────
        for off, clen in chunk_map.get(MAGIC_NJTL, []):
            self.bs = BitStream(data[off : off + clen], big_endian=big_endian)
            try: self._readList()
            except Exception: pass   # malformed NJTL is non-fatal
        for idx, rn in enumerate(self.texNames):
            if idx < len(self.textures):
                c = os.path.splitext(os.path.basename(rn))[0]
                if c: self.textures[idx]['name'] = c

        # ── POF0 relocation: patch NJCM payload if a relocation table exists ──
        # Some NJ files (notably certain GC boss models) are compiled with a
        # non-zero serialization base, meaning their pointer values include an
        # extra addend that must be subtracted before the offsets are usable.
        # POF0 lists exactly which 32-bit words in the NJCM payload are pointers
        # so we can patch them without guessing the struct layout.
        pof0_chunks = chunk_map.get(MAGIC_POF0, [])

        for off, clen in chunk_map.get(MAGIC_NJCM, []):
            njcm_bytes = data[off : off + clen]
            if pof0_chunks:
                pof0_off, pof0_clen = pof0_chunks[0]
                pof0_bytes = data[pof0_off : pof0_off + pof0_clen]
                njcm_bytes = apply_pof0_relocation(njcm_bytes, pof0_bytes, big_endian)
            self.bs = BitStream(njcm_bytes, big_endian=big_endian)
            self._readBone()
            break   # only the first NJCM is geometry

    def _readList(self):
        lofs  = self.bs.readUInt()
        count = self.bs.readUInt()
        self.bs.seek(lofs)
        sofs  = []
        for _ in range(count):
            sofs.append(self.bs.readUInt()); self.bs.seek(8, 1)
        for o in sofs:
            self.bs.seek(o); self.texNames.append(self.bs.readString())

    def _readBone(self, pMatrix=None):
        # Non-quaternion node = 52 bytes; quaternion = 56. Bail early if we
        # don't have enough buffer left to read the smaller header.
        if self.bs.pos + 52 > self.bs.getSize():
            return
        c     = 2.0 * math.pi / 0x10000
        flags = self.bs.readUInt()
        if flags & 0x400:
            if self.bs.pos + 52 > self.bs.getSize():   # need 4 more bytes for 'w'
                return
            node = {'meshOfs': self.bs.readUInt(),
                    'pos': (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
                    'rot': (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
                    'scl': (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
                    'childOfs': self.bs.readUInt(), 'siblingOfs': self.bs.readUInt(),
                    'w': self.bs.readFloat()}
        else:
            node = {'meshOfs': self.bs.readUInt(),
                    'pos': (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
                    'rot': (self.bs.readInt()*c, self.bs.readInt()*c, self.bs.readInt()*c),
                    'scl': (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
                    'childOfs': self.bs.readUInt(), 'siblingOfs': self.bs.readUInt()}
        mat = DashMat4()
        if not (flags & 0x02):
            if flags & 0x400: mat.rotate4(node['rot'], node['w'])
            else:             mat.rotate(node['rot'])
        if not (flags & 0x01): mat.translate(node['pos'])
        if pMatrix is not None: mat.compose(pMatrix)
        self.current_matrix = mat

        sz = self.bs.getSize()
        if node['meshOfs'] >= sz or node['childOfs'] >= sz or node['siblingOfs'] >= sz: return
        if node['meshOfs']    != 0: self.bs.seek(node['meshOfs']);    self._readMesh()
        if node['childOfs']   != 0: self.bs.seek(node['childOfs']);   self._readBone(mat)
        if node['siblingOfs'] != 0: self.bs.seek(node['siblingOfs']); self._readBone(pMatrix)

    def _readMesh(self):
        vofs = self.bs.readUInt(); cofs = self.bs.readUInt()
        self.bs.readBytes(16)   # center (3 floats) + radius
        sz = self.bs.getSize()
        if vofs != 0 and vofs < sz: self.bs.seek(vofs); self.readChunks(self.bs)
        if cofs != 0 and cofs < sz: self.bs.seek(cofs); self.readChunks(self.bs)


# ============================================================
# DC .rel stage importer
# ============================================================
class NinjaDCRelImporter(NinjaChunkMixin):

    def __init__(self):
        self.texNames       = []
        self.sections       = {}
        self.vertex_stack   = {}
        self.materials_data = []
        self.meshes_data    = []
        self.textures       = []
        self.current_matrix = DashMat4()
        self.material       = {}
        self.store_ofs      = [None] * 256
        self.jump_to        = 0
        self.bs_d = self.bs_n = self.bs = None

    def setTextures(self, textures): self.textures = textures

    def parse(self, d_data, n_data):
        if d_data: self.bs_d = BitStream(d_data)
        if n_data: self.bs_n = BitStream(n_data)
        if self.bs_d: self._prepare(self.bs_d, 'd')
        if self.bs_n: self._prepare(self.bs_n, 'n')
        self._readSections()

    def _prepare(self, bs, label):
        bs.seek(bs.getSize() - 16)
        table_ofs = bs.readUInt()
        bs.seek(table_ofs)
        section_count = bs.readUInt()
        bs.readUInt()                      # magic
        section_ofs   = bs.readUInt()
        texture_ofs   = bs.readUInt()

        # Texture names embedded in the .rel file
        bs.seek(texture_ofs)
        tn_ofs   = bs.readUInt()
        tn_count = bs.readUInt()
        bs.seek(tn_ofs)
        for i in range(tn_count):
            name_ofs = bs.readUInt()
            save_ofs = bs.tell() + 8
            bs.seek(name_ofs); name = bs.readString(); bs.seek(save_ofs)
            if i >= len(self.texNames): self.texNames.append(name)
        for idx, tn in enumerate(self.texNames):
            if idx < len(self.textures):
                c = os.path.splitext(os.path.basename(tn))[0]
                if c: self.textures[idx]['name'] = c

        c = 2.0 * math.pi / 0x10000
        bs.seek(section_ofs)
        for _ in range(section_count):
            sid = bs.readInt()
            pos = [bs.readFloat(), bs.readFloat(), bs.readFloat()]
            rot = [bs.readInt()*c, bs.readInt()*c, bs.readInt()*c]
            bs.readFloat()                 # radius
            a_ofs = bs.readUInt(); b_ofs = bs.readUInt(); c_ofs = bs.readUInt()
            a_num = bs.readUInt(); b_num = bs.readUInt(); c_num = bs.readUInt()
            bs.readUInt()                  # end
            save = bs.tell()
            key  = str(sid)
            if key not in self.sections:
                self.sections[key] = {'pos': pos, 'rot': rot, 'static': [], 'animated': []}
            bs.seek(a_ofs)
            for _ in range(a_num):
                m_ofs = bs.readUInt(); bs.readBytes(0x2c)
                self.sections[key]['static'].append({'src': label, 'm_ofs': m_ofs})
            bs.seek(c_ofs)
            for _ in range(c_num):
                m_ofs = bs.readUInt(); bs.readUInt(); bs.readBytes(0x34)
                self.sections[key]['animated'].append({'src': label, 'm_ofs': m_ofs})
            bs.seek(save)

    def _readSections(self):
        for key, section in self.sections.items():
            mat = DashMat4()
            mat.rotate(section['rot']); mat.translate(section['pos'])
            for e in section['static'] + section['animated']:
                self.bs = self.bs_d if e['src'] == 'd' else self.bs_n
                if self.bs is None: continue
                self.bs.seek(e['m_ofs'])
                self.vertex_stack = {}
                self._readNode(mat)

    def _readNode(self, pMatrix=None):
        if self.bs.pos + 52 > self.bs.getSize():
            return
        c = 2.0 * math.pi / 0x10000
        node = {'flags':      self.bs.readUInt(), 'meshOfs': self.bs.readUInt(),
                'pos':        (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
                'rot':        (self.bs.readInt()*c, self.bs.readInt()*c, self.bs.readInt()*c),
                'scl':        (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()),
                'childOfs':   self.bs.readUInt(), 'siblingOfs': self.bs.readUInt()}
        mat = DashMat4()
        if not (node['flags'] & 0x02): mat.rotate(node['rot'])
        if not (node['flags'] & 0x01): mat.translate(node['pos'])
        if pMatrix is not None: mat.compose(pMatrix)
        self.current_matrix = mat

        sz = self.bs.getSize()
        if node['meshOfs'] >= sz or node['childOfs'] >= sz or node['siblingOfs'] >= sz: return
        if node['meshOfs']    != 0: self.bs.seek(node['meshOfs']);    self._readMesh()
        if node['childOfs']   != 0: self.bs.seek(node['childOfs']);   self._readNode(mat)
        if node['siblingOfs'] != 0: self.bs.seek(node['siblingOfs']); self._readNode(pMatrix)

    def _readMesh(self):
        vofs = self.bs.readUInt(); cofs = self.bs.readUInt()
        self.bs.readBytes(16)
        if vofs != 0: self.bs.seek(vofs); self.readChunks(self.bs)
        if cofs != 0: self.bs.seek(cofs); self.readChunks(self.bs)


# ============================================================
# GC .gj model importer  (big-endian Flipper format)
# Textures: GVR decode is not yet implemented — geometry only.
# ============================================================
class FlipperGCImporter:

    def __init__(self):
        self.texNames       = []
        self.vertex_stack   = {}
        self.materials_data = []
        self.meshes_data    = []
        self.textures       = []
        self.current_matrix = DashMat4()
        self.material       = {}
        self.file_ofs       = 0       # abs offset of GJCM chunk start in main bs
        self._stop          = False
        self._face_flags    = 0

    def setTextures(self, textures): self.textures = textures

    def parse(self, data):
        outer = BitStream(data)          # outer stream is little-endian for magic scan
        size  = outer.getSize() - 4
        while outer.tell() < size:
            magic = outer.readUInt()
            if magic == MAGIC_GJTL:
                chunk_len = outer.readUInt()
                self.bs   = BitStream(outer.readBytes(chunk_len), big_endian=True)
                self._readTexList()
            elif magic == MAGIC_GJCM:
                chunk_len      = outer.readUInt()
                self.file_ofs  = outer.tell()
                self.bs        = BitStream(outer.readBytes(chunk_len), big_endian=True)
                self._readNode()
                break
            else:
                try: outer.readBytes(outer.readUInt())
                except: break

    def _readTexList(self):
        lofs  = self.bs.readUInt()
        count = self.bs.readUInt()
        self.bs.seek(lofs)
        sofs  = []
        for _ in range(count):
            sofs.append(self.bs.readUInt()); self.bs.readBytes(8)
        for o in sofs:
            self.bs.seek(o); self.texNames.append(self.bs.readString())

    def _readNode(self, pNode=None):
        if self._stop: return
        c = 2.0 * math.pi / 0x10000
        node = {'flags':      self.bs.readUInt(), 'meshOfs': self.bs.readUInt(),
                'pos':        [self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()],
                'rot':        [self.bs.readInt()*c, self.bs.readInt()*c, self.bs.readInt()*c],
                'scl':        [self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat()],
                'childOfs':   self.bs.readUInt(), 'siblingOfs': self.bs.readUInt()}
        mat = DashMat4()
        if not (node['flags'] & 0x02): mat.rotate(node['rot'])
        if not (node['flags'] & 0x01): mat.translate(node['pos'])
        pmat = pNode['matrix'] if pNode else None
        if pmat is not None: mat.compose(pmat)
        self.current_matrix = mat
        bone = {'matrix': mat}

        sz = self.bs.getSize()
        if node['meshOfs']    != 0 and node['meshOfs']    <= sz:
            self.bs.seek(node['meshOfs']); self._readMesh()
        if node['childOfs']   != 0 and node['childOfs']   <= sz:
            self.bs.seek(node['childOfs']); self._readNode(bone)
        if node['siblingOfs'] != 0 and node['siblingOfs'] <= sz:
            self.bs.seek(node['siblingOfs']); self._readNode(pNode)

    def _readMesh(self):
        prop_ofs        = self.bs.readUInt()
        self.bs.readUInt()             # zero
        strip_ofs       = self.bs.readUInt()
        astrip_ofs      = self.bs.readUInt()
        strip_count     = self.bs.readShort()
        astrip_count    = self.bs.readShort()
        self.bs.readBytes(16)          # center + radius

        self._pos=[]; self._norm=[]; self._color=[]; self._uv=[]
        if prop_ofs != 0:
            self.bs.seek(prop_ofs); self._readProps()
        if strip_count  != 0 and strip_ofs  != 0:
            self.bs.seek(strip_ofs);  self._readStrips(False, strip_count)
        if astrip_count != 0 and astrip_ofs != 0:
            self.bs.seek(astrip_ofs); self._readStrips(True,  astrip_count)

    def _readProps(self):
        attrs = []
        while True:
            t = self.bs.readUByte()
            if t == 0xFF: break
            attrs.append({'type': t, 'size': self.bs.readUByte(),
                          'count': self.bs.readUShort(), 'unknown': self.bs.readUInt(),
                          'offset': self.bs.readUInt(), 'length': self.bs.readUInt()})
        TYPE = {1:'POS', 2:'NORM', 3:'COLOR', 5:'UV'}
        for a in attrs:
            self.bs.seek(a['offset'])
            tp = TYPE.get(a['type'])
            for _ in range(a['count']):
                if   tp == 'POS':
                    x,y,z = struct.unpack_from('>fff', self.bs.readBytes(12))
                    self._pos.append(self.current_matrix.transformPoint((x,y,z)))
                elif tp == 'NORM':
                    x,y,z = struct.unpack_from('>fff', self.bs.readBytes(12))
                    self._norm.append(self.current_matrix.transformNormal((x,y,z)))
                elif tp == 'COLOR':
                    # GC RGBA8: one big-endian uint32 packs all four channels.
                    # Byte order in the stream is [R, G, B, A].
                    v  = self.bs.readUInt()
                    r2 = ((v >> 24) & 0xFF) / 255.0
                    g2 = ((v >> 16) & 0xFF) / 255.0
                    b2 = ((v >>  8) & 0xFF) / 255.0
                    a2 = ( v        & 0xFF) / 255.0
                    self._color.append((r2, g2, b2, a2))
                elif tp == 'UV':
                    u = self.bs.readShort()/255.0; v = self.bs.readShort()/255.0
                    self._uv.append((u, 1.0-v))
                else:
                    self.bs.readBytes(a['size'])

    def _readStrips(self, use_alpha, count):
        polygons = []
        for _ in range(count):
            polygons.append({'mat_ofs':   self.bs.readUInt(), 'mat_count': self.bs.readUInt(),
                             'strip_ofs': self.bs.readUInt(), 'strip_len': self.bs.readUInt()})
        self.material = {'diffuse':(1,1,1,1),'ambient':(1,1,1,1),'specular':(1,1,1,1),
                         'texIndex':-1,'blendSrc':'','blendDst':'','doubleSided':False}
        for pg in polygons:
            if self._stop: return
            self.bs.seek(pg['mat_ofs']); self._readGCMat(pg['mat_count'])
            self.bs.seek(pg['strip_ofs']); self._readIndices(pg['strip_len'])

    def _readGCMat(self, count):
        for _ in range(count):
            t = self.bs.readUByte(); self.bs.readBytes(3); val = self.bs.readUInt()
            if t == 0x01: self._face_flags = val
            elif t == 0x08:
                tid = val & 0x1FFF
                self.material['texIndex'] = tid if tid < len(self.texNames) else -1

    def _readIndices(self, byte_len):
        ff      = self._face_flags
        # Each GX vertex attribute encodes its index type in its own 2-bit field:
        #   bits 3:2 = pos, 5:4 = norm, 7:6 = color, 11:10 = uv
        #   value 10 = 8-bit index, 11 = 16-bit index (low bit of each pair)
        fmt = []
        if ff & 0x8:   fmt.append(('pos',   2 if (ff & 0x04)  else 1))
        if ff & 0x20:  fmt.append(('norm',  2 if (ff & 0x10)  else 1))
        if ff & 0x80:  fmt.append(('color', 2 if (ff & 0x40)  else 1))
        if ff & 0x800: fmt.append(('uv',    2 if (ff & 0x400) else 1))

        end_ofs = self.bs.tell() + byte_len
        attrs = {'pos':[], 'norm':[], 'color':[], 'uv':[], 'tri':[], 'ofs':0}

        while self.bs.tell() < end_ofs:
            prim = self.bs.readUByte()
            if prim == 0x00: continue
            if prim not in (0x90, 0x98):
                self._stop = True; return
            cnt = self.bs.readUShort()
            fan = []
            for _ in range(cnt):
                vert = {}
                for ch, isize in fmt:
                    i = self.bs.readUShort() if isize == 2 else self.bs.readUByte()
                    if   ch == 'pos'   and i < len(self._pos):   vert['pos']   = self._pos[i]
                    elif ch == 'norm'  and i < len(self._norm):  vert['norm']  = self._norm[i]
                    elif ch == 'color' and i < len(self._color): vert['color'] = self._color[i]
                    elif ch == 'uv'    and i < len(self._uv):    vert['uv']    = self._uv[i]
                fan.append(vert)
            # 0x98 = fan, 0x90 = triangles
            if prim == 0x98:
                cw = True
                for i in range(len(fan)-2):
                    if cw: a,b,c = i,i+2,i+1
                    else:  a,b,c = i+1,i+2,i
                    cw = not cw
                    attrs['tri'].extend([a+attrs['ofs'], b+attrs['ofs'], c+attrs['ofs']])
            else:
                for i in range(len(fan)//3):
                    attrs['tri'].extend([3*i+attrs['ofs'], 3*i+2+attrs['ofs'], 3*i+1+attrs['ofs']])
            for vt in fan:
                attrs['pos'].append(vt.get('pos',(0,0,0)))
                if 'norm'  in vt: attrs['norm'].append(vt['norm'])
                if 'color' in vt: attrs['color'].append(vt['color'])
                if 'uv'    in vt: attrs['uv'].append(vt['uv'])
            attrs['ofs'] += len(fan)

        if not attrs['pos']: return
        has_vc  = bool(attrs['color'])
        mat_key = (self.material['diffuse'], self.material['texIndex'],
                   self.material['blendSrc'], self.material['blendDst'], has_vc)
        mi = next((i for i,m in enumerate(self.materials_data) if m['key']==mat_key), None)
        if mi is None:
            mi = len(self.materials_data)
            self.materials_data.append({
                'key': mat_key, 'name': "mat_%03d" % mi,
                'diffuse': self.material['diffuse'], 'texIndex': self.material['texIndex'],
                'blendSrc': self.material['blendSrc'], 'blendDst': self.material['blendDst'],
                'doubleSided': False, 'has_vertex_colors': has_vc,
            })
        self.meshes_data.append({
            'positions': attrs['pos'], 'normals': attrs['norm'], 'colors': attrs['color'],
            'uvs': attrs['uv'], 'triangles': attrs['tri'], 'mat_index': mi,
        })

    # ------------------------------------------------------------------
    def parse_stage(self, data):
        """Parse a PSO GameCube n.rel stage file (big-endian Flipper format)."""
        self.bs = BitStream(data, big_endian=True)

        # Footer: last 16 bytes, first uint is table offset
        self.bs.seek(self.bs.getSize() - 16)
        table_ofs = self.bs.readUInt()

        self.bs.seek(table_ofs)
        self.bs.readUInt()              # fmt2
        self.bs.readUInt()              # n_count (unused)
        d_count = self.bs.readUShort()
        self.bs.readUShort()            # padding
        self.bs.readUInt()              # hd
        d_ofs   = self.bs.readUInt()
        tex_ofs = self.bs.readUInt()

        # Texture names
        self.bs.seek(tex_ofs)
        tn_ofs   = self.bs.readUInt()
        tn_count = self.bs.readUInt()
        self.bs.seek(tn_ofs)
        for i in range(tn_count):
            name_ofs = self.bs.readUInt()
            save_ofs = self.bs.tell() + 8
            self.bs.seek(name_ofs)
            name = self.bs.readString()
            self.bs.seek(save_ofs)
            self.texNames.append(name)
            if i < len(self.textures):
                clean = os.path.splitext(name)[0] or name
                self.textures[i]['name'] = clean

        # Section table
        c = 2.0 * math.pi / 0x10000
        SKIP_A = {0x010225, 0x010204, 0x010205, 0x010264}
        SKIP_B = {0x010244, 0x010204}

        self.bs.seek(d_ofs)
        save_pos = self.bs.tell()
        for _ in range(d_count):
            self.bs.seek(save_pos)
            self.bs.readInt()   # section id
            pos3  = (self.bs.readFloat(), self.bs.readFloat(), self.bs.readFloat())
            rot3  = (self.bs.readInt()*c, self.bs.readInt()*c, self.bs.readInt()*c)
            self.bs.readFloat() # radius
            ptr_a   = self.bs.readUInt()
            ptr_b   = self.bs.readUInt()
            cnt_a   = self.bs.readUInt()
            cnt_b   = self.bs.readUInt()
            self.bs.readUInt()  # end
            save_pos = self.bs.tell()

            sec_mat = DashMat4()
            sec_mat.rotate(rot3)
            sec_mat.translate(pos3)
            parent = {'matrix': sec_mat}

            # Read list_a (static meshes)
            list_a = []
            self.bs.seek(ptr_a)
            for _ in range(cnt_a):
                m = self.bs.readUInt()
                self.bs.readUInt(); self.bs.readUInt()   # attr1, attr2
                f = self.bs.readUInt()
                list_a.append((m, f))

            # Read list_b (animated meshes)
            list_b = []
            self.bs.seek(ptr_b)
            for _ in range(cnt_b):
                m = self.bs.readUInt()
                self.bs.readUInt()          # a_ofs
                self.bs.readBytes(8)
                self.bs.readFloat()         # speed
                self.bs.readBytes(8)
                f = self.bs.readUInt()
                list_b.append((m, f))

            for m_ofs, flags in list_a:
                if flags in SKIP_A or flags & 0x200: continue
                self._stop = False
                self.bs.seek(m_ofs)
                self._readNode(parent)

            for m_ofs, flags in list_b:
                if flags in SKIP_B or flags & 0x200: continue
                self._stop = False
                self.bs.seek(m_ofs)
                self._readNode(parent)


# ============================================================
# PRS decompressor  (SEGA LZS variant used in BML archives)
# ============================================================
def decompress_prs(data):
    """Decompress SEGA PRS/LZS compressed data. Returns raw bytes."""
    import array as _array
    ibuf = _array.array('B', data)
    obuf = _array.array('B')
    iofs = [0]; bit_count = [0]; cmd_byte = [0]

    def _byte():
        v = ibuf[iofs[0]]; iofs[0] += 1; return v

    def _bit():
        if bit_count[0] == 0:
            cmd_byte[0] = _byte(); bit_count[0] = 8
        b = cmd_byte[0] & 1; cmd_byte[0] >>= 1; bit_count[0] -= 1
        return b

    while iofs[0] < len(ibuf):
        if _bit():
            obuf.append(ibuf[iofs[0]]); iofs[0] += 1
        else:
            if _bit():
                a = _byte(); b = _byte()
                offset = ((b << 8) | a) >> 3
                amount = a & 7
                if iofs[0] < len(ibuf):
                    amount = (_byte() + 1) if amount == 0 else (amount + 2)
                start = len(obuf) - 0x2000 + offset
            else:
                amount = (_bit() << 1) | _bit()
                offset = _byte(); amount += 2
                start  = len(obuf) - 0x100 + offset
            for _ in range(amount):
                obuf.append(obuf[start] if 0 <= start < len(obuf) else 0)
                start += 1

    return bytes(obuf)


# ============================================================
# BML archive reader
# ============================================================
_MODEL_EXTS   = {'.nj', '.gj', '.xj'}
_TEXTURE_EXTS = {'.pvm', '.gvm'}
_ANIM_EXTS    = {'.njm', '.gjm'}

def bml_read(data):
    """
    Parse a BML archive (DC / GC PSO model bundle).
    Returns a list of dicts: {'filename': str, 'data': bytes}
    in archive order.  All entries are returned; callers decide what to skip.
    Auto-detects big-endian (GC) vs little-endian (DC/PC) from the count field.
    """
    if len(data) < 0x80:
        return []

    # Detect endianness: count field at offset 4 must be a small positive int
    count_be = struct.unpack_from('>i', data, 4)[0]
    count_le = struct.unpack_from('<i', data, 4)[0]
    sane_be  = 0 < count_be <= 2000
    sane_le  = 0 < count_le <= 2000
    if sane_be and not sane_le:
        bo, count = '>', count_be
    elif sane_le and not sane_be:
        bo, count = '<', count_le
    else:
        # Both look plausible — GC files default to big-endian
        bo, count = ('>', count_be) if sane_be else ('<', count_le)

    # Parse file-entry table at 0x40 (each slot is 0x40 bytes)
    pos     = 0x40
    entries = []
    for _ in range(count):
        if pos + 0x40 > len(data):
            break
        s = struct.unpack_from(bo + '32sIIIII', data, pos)
        pos += 0x34 + 0x0C          # entry fields + 12-byte padding
        name        = s[0].decode('ascii', errors='ignore').rstrip(' \t\r\n\0')
        comp_size   = s[1]
        decomp_size = s[3]
        pvm_comp    = s[4]
        pvm_decomp  = s[5]
        entries.append({'filename': name, 'compressed_size': comp_size,
                        'decompressed_size': decomp_size})
        if pvm_comp:
            # Texture archive immediately follows the model in the data stream.
            # Name the texture by stripping the model extension; use .gvm for
            # big-endian (GC) archives, .pvm otherwise.
            basename = os.path.splitext(name)[0]
            tex_ext  = '.gvm' if bo == '>' else '.pvm'
            entries.append({'filename': basename + tex_ext,
                            'compressed_size': pvm_comp,
                            'decompressed_size': pvm_decomp})

    # Compressed data starts at the next 0x800-aligned offset after the table.
    # Round up: when pos is already 0x800-aligned, stay there (don't add another block).
    ofs = (pos + 0x7FF) & 0xFFFFF800

    # Decompress each entry; null bytes between entries are padding
    result = []
    for e in entries:
        while ofs < len(data) and data[ofs] == 0:
            ofs += 1
        if ofs >= len(data):
            break
        raw = data[ofs: ofs + e['compressed_size']]
        ofs += e['compressed_size']
        try:
            dec = decompress_prs(raw)
        except Exception:
            dec = b''
        result.append({'filename': e['filename'], 'data': dec})

    return result


# ============================================================
# Eevee shadow helper
# ============================================================
def disable_eevee_shadows():
    """Disable Eevee shadows on every scene (scene name is user-dependent)."""
    for scene in bpy.data.scenes:
        try:
            scene.eevee.use_shadows = False
        except Exception:
            pass

# ============================================================
# Viewport clip distance helper
# ============================================================
def extend_clip_distance(geo):
    """
    Find the farthest vertex coordinate in the imported geometry and ensure
    every 3D Viewport's Clip End is at least twice that distance.
    Works regardless of how many screens/areas the user has open.
    """
    max_coord = 0.0
    for md in geo.meshes_data:
        for pos in md['positions']:
            for coord in pos:
                v = abs(coord)
                if v > max_coord:
                    max_coord = v

    if max_coord <= 0.0:
        return

    # Double the farthest extent so geometry doesn't clip right at its edge
    needed = max_coord * 2.0

    updated = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D' and space.clip_end < needed:
                        space.clip_end = needed
                        updated += 1

    if updated:
        print("[PSO importer] Clip End set to %.1f across %d viewport(s)" % (needed, updated))


# ============================================================
# XVM auto-detection
# ============================================================
def find_xvm_path(rel_filepath):
    """
    Return the best-guess .xvm path for a given n.rel path, or None.

    PSO BB stage files follow naming conventions like:
        forest_01n.rel  ->  forest.xvm   (2-digit suffix: [-3] == '_')
        city01_0n.rel   ->  city01.xvm   (1-digit suffix: [-2] == '_')
        lobbn.rel       ->  lobb.xvm     (no suffix)

    We try every plausible strip length rather than hard-coding one rule,
    then fall back to any single .xvm found in the same directory.
    """
    # Strip the trailing "n.rel" (5 chars) to get the base stem
    stem = rel_filepath[:-5]          # e.g. ".../forest_01"
    directory = os.path.dirname(rel_filepath)

    candidates = []

    # Try stripping underscore+suffix of length 1, 2, or 3 from the stem
    for strip in range(1, 4):
        if len(stem) > strip and stem[-(strip)] == '_':
            candidates.append(stem[:-(strip)] + ".xvm")

    # Try the stem itself (no suffix stripping)
    candidates.append(stem + ".xvm")

    for path in candidates:
        if os.path.exists(path):
            return path

    # Last resort: find the only .xvm in the same directory
    try:
        xvms = [f for f in os.listdir(directory) if f.lower().endswith('.xvm')]
        if len(xvms) == 1:
            return os.path.join(directory, xvms[0])
    except OSError:
        pass

    return None


def find_pvm_path(filepath):
    """Locate a .pvm texture archive for a DC .nj or DC .rel file."""
    stem = os.path.splitext(filepath)[0]
    # DC .rel files end in [d|n|c|r].rel — try stripping the type letter
    if stem and stem[-1] in ('d', 'n', 'c', 'r'):
        base = stem[:-1]
        for p in (base + ".pvm", stem + ".pvm"):
            if os.path.exists(p): return p
    else:
        p = stem + ".pvm"
        if os.path.exists(p): return p
    # Fallback: only .pvm in same directory
    try:
        d   = os.path.dirname(filepath)
        pvms = [f for f in os.listdir(d) if f.lower().endswith('.pvm')]
        if len(pvms) == 1: return os.path.join(d, pvms[0])
    except OSError: pass
    return None


def find_gvm_path(filepath):
    """Locate a .gvm texture archive for a GC .gj model or GC n.rel stage file."""
    stem = os.path.splitext(filepath)[0]
    # GC n.rel files end in 'n' before the extension — try stripping it
    if stem and stem[-1] == 'n':
        base = stem[:-1]
        for p in (base + ".gvm", stem + ".gvm"):
            if os.path.exists(p): return p
    else:
        p = stem + ".gvm"
        if os.path.exists(p): return p
    # Fallback: only .gvm in same directory
    try:
        d    = os.path.dirname(filepath)
        gvms = [f for f in os.listdir(d) if f.lower().endswith('.gvm')]
        if len(gvms) == 1: return os.path.join(d, gvms[0])
    except OSError: pass
    return None


def find_compound_tex_path(model_filepath):
    """Check for a compound-extension texture archive beside a model file.

    For a model at 'path/model.nj', checks (in order):
        path/model.nj.xvm  path/model.nj.XVM
        path/model.nj.gvm  path/model.nj.GVM
        path/model.nj.pvm  path/model.nj.PVM
    Works for any model extension (.nj, .xj, .gj, …).
    Returns the first path that exists, or None.
    """
    for tex_ext in ('.xvm', '.XVM', '.gvm', '.GVM', '.pvm', '.PVM'):
        candidate = model_filepath + tex_ext
        if os.path.exists(candidate):
            return candidate
    return None


# ============================================================
# Shared operator mix-in for common import settings
# ============================================================
def _common_props():
    """Returns a dict of property descriptors shared across all operators."""
    return {}   # used structurally below


# ============================================================
# Blender Import Operator — PSO BB n.rel
# ============================================================
class IMPORT_OT_pso_rel(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_rel"
    bl_label       = "Import PSO n.rel"
    bl_description = "Import a Phantasy Star Online Blue Burst n.rel stage file"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ".rel"
    filter_glob: StringProperty(default="*n.rel", options={'HIDDEN'})

    xvm_filepath: StringProperty(
        name="Texture Archive (.xvm)",
        description="Path to the .xvm texture archive. Leave blank to auto-detect",
        default="",
        subtype='FILE_PATH',
    )

    blend_vertex_colors: BoolProperty(
        name="Blend Vertex Colors",
        description=(
            "Apply vertex colors as lighting in the scene"
        ),
        default=True,
    )

    disable_color_correction: BoolProperty(
        name="Disable Color Correction",
        description=(
            "Set the scene Color Management transform to Standard, disabling Filmic/AgX "
            "tonemapping so textures match the original game's appearance"
        ),
        default=True,
    )

    extend_clip_distance: BoolProperty(
        name="Extend Viewport Clip Distance",
        description=(
            "Increase 3D Viewport's Clip End so the imported mesh is fully visible. "
        ),
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Texture Archive:")
        layout.prop(self, "xvm_filepath", text="")
        layout.label(text="(leave blank to auto-detect)")
        layout.separator()
        layout.prop(self, "blend_vertex_colors")
        layout.prop(self, "disable_color_correction")
        layout.prop(self, "extend_clip_distance")

    def execute(self, context):
        filepath = self.filepath

        # Load the .rel file
        try:
            with open(filepath, 'rb') as f:
                rel_data = f.read()
        except OSError as e:
            self.report({'ERROR'}, "Cannot open file: %s" % e)
            return {'CANCELLED'}

        # Resolve .xvm path: manual override > auto-detect
        xvm_path = self.xvm_filepath.strip() or find_xvm_path(filepath)

        textures = []
        if xvm_path and os.path.exists(xvm_path):
            try:
                with open(xvm_path, 'rb') as f:
                    xvr_data = f.read()
                textures = xvr_load(xvr_data)
                msg = "Loaded %d texture(s) from %s" % (len(textures), os.path.basename(xvm_path))
                self.report({'INFO'}, msg)
                print("[PSO n.rel] " + msg)
            except Exception as e:
                self.report({'WARNING'}, "Texture load failed: %s" % e)
                print("[PSO n.rel] Texture load failed: %s" % e)
        else:
            tried = xvm_path or "(no candidate found)"
            self.report({'WARNING'}, "XVM not found — tried: %s" % tried)
            print("[PSO n.rel] XVM not found — tried: %s" % tried)

        # Parse geometry
        geo = NinjaStageGeometry()
        geo.setTextures(textures)
        try:
            geo.parse(rel_data)
        except Exception as e:
            self.report({'ERROR'}, "Parse error: %s" % e)
            print("[PSO n.rel] Parse error: %s" % e)
            return {'CANCELLED'}

        # Build Blender scene
        try:
            mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e)
            print("[PSO n.rel] Scene build error: %s" % e)
            return {'CANCELLED'}

        if self.extend_clip_distance:
            extend_clip_distance(geo)

        if self.blend_vertex_colors:
            disable_eevee_shadows()

        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = "Standard"
                print("[PSO n.rel] Color management set to Standard")
            except Exception as e:
                self.report({'WARNING'}, "Could not set color management: %s" % e)
                print("[PSO n.rel] Could not set color management: %s" % e)

        result = "Imported %d mesh(es), %d texture(s) from %s" % (
            mesh_count, len(textures), os.path.basename(filepath)
        )
        self.report({'INFO'}, result)
        print("[PSO n.rel] " + result)
        return {'FINISHED'}

# ============================================================
# XJ Import Operator
# ============================================================
class IMPORT_OT_pso_xj(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_xj"
    bl_label       = "Import PSO BB .xj"
    bl_description = "Import a Phantasy Star Online Blue Burst .xj prop/character model file"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ".xj"
    filter_glob: StringProperty(default="*.xj", options={'HIDDEN'})

    xvm_filepath: StringProperty(
        name="Texture Archive (.xvm)",
        description="Path to the .xvm texture archive. Leave blank to auto-detect",
        default="",
        subtype='FILE_PATH',
    )

    blend_vertex_colors: BoolProperty(
        name="Blend Vertex Colors",
        description=(
            "Apply vertex colors as lighting in the scene"
        ),
        default=True,
    )

    disable_color_correction: BoolProperty(
        name="Disable Color Correction",
        description=(
            "Set the scene Color Management transform to Standard, disabling Filmic/AgX "
            "tonemapping so textures match the original game's appearance"
        ),
        default=True,
    )

    extend_clip_distance: BoolProperty(
        name="Extend Viewport Clip Distance",
        description=(
            "Increase every 3D Viewport's Clip End so the imported mesh is fully visible. "
            "PSO models can exceed Blender's default clip distance of 1000"
        ),
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Texture Archive:")
        layout.prop(self, "xvm_filepath", text="")
        layout.label(text="(leave blank to auto-detect)")
        layout.separator()
        layout.prop(self, "blend_vertex_colors")
        layout.prop(self, "disable_color_correction")
        layout.prop(self, "extend_clip_distance")

    def execute(self, context):
        filepath = self.filepath

        # Load the .xj file
        try:
            with open(filepath, 'rb') as f:
                xj_data = f.read()
        except OSError as e:
            self.report({'ERROR'}, "Cannot open file: %s" % e)
            return {'CANCELLED'}

        # XJ texture archive: compound extension first, then same base name
        tex_path = self.xvm_filepath.strip()
        if not tex_path:
            tex_path = find_compound_tex_path(filepath)
        if not tex_path:
            stem = os.path.splitext(filepath)[0]
            xvm_candidate = stem + ".xvm"
            pvm_candidate = stem + ".pvm"
            if os.path.exists(xvm_candidate):
                tex_path = xvm_candidate
            elif os.path.exists(pvm_candidate):
                tex_path = pvm_candidate

        textures = []
        if tex_path and os.path.exists(tex_path):
            try:
                with open(tex_path, 'rb') as f:
                    tex_data = f.read()
                textures = load_texture_archive(tex_data)
                msg = "Loaded %d texture(s) from %s" % (len(textures), os.path.basename(tex_path))
                self.report({'INFO'}, msg)
                print("[PSO .xj] " + msg)
            except Exception as e:
                self.report({'WARNING'}, "Texture load failed: %s" % e)
                print("[PSO .xj] Texture load failed: %s" % e)
        else:
            tried = tex_path or os.path.splitext(filepath)[0] + ".xvm/.pvm"
            self.report({'WARNING'}, "Texture archive not found — tried: %s" % tried)
            print("[PSO .xj] Texture archive not found — tried: %s" % tried)

        # Parse geometry
        geo = NinjaXJImporter()
        geo.setTextures(textures)
        try:
            geo.parse(xj_data)
        except Exception as e:
            self.report({'ERROR'}, "Parse error: %s" % e)
            print("[PSO .xj] Parse error: %s" % e)
            return {'CANCELLED'}

        # Build Blender scene
        try:
            mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e)
            print("[PSO .xj] Scene build error: %s" % e)
            return {'CANCELLED'}

        if self.extend_clip_distance:
            extend_clip_distance(geo)

        if self.blend_vertex_colors:
            disable_eevee_shadows()

        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = "Standard"
            except Exception as e:
                self.report({'WARNING'}, "Could not set color management: %s" % e)

        result = "Imported %d mesh(es), %d texture(s) from %s" % (
            mesh_count, len(textures), os.path.basename(filepath)
        )
        self.report({'INFO'}, result)
        print("[PSO .xj] " + result)
        return {'FINISHED'}


# ============================================================
# Helper: build a standard operator draw/execute body for DC/GC imports
# ============================================================
def _make_pvm_operator_body(operator, context, geo_class, file_data,
                             filepath, label):
    """Shared execute logic for DC .nj, DC .rel, and GC .gj operators.

    Tries .pvm first, then .gvm, so GC NJ files whose companion texture archive
    uses the .gvm extension (e.g. 'model.nj.gvm') are also picked up.
    load_texture_archive() auto-detects PVMH vs GVMH, so either format works.
    """
    manual = operator.xvm_filepath.strip()
    tex_path = (manual
                or find_compound_tex_path(filepath)
                or find_pvm_path(filepath)
                or find_gvm_path(filepath))
    textures = []
    if tex_path and os.path.exists(tex_path):
        try:
            with open(tex_path, 'rb') as f:
                raw = f.read()
            textures = load_texture_archive(raw)
            msg = "Loaded %d texture(s) from %s" % (len(textures), os.path.basename(tex_path))
            operator.report({'INFO'}, msg); print("[%s] %s" % (label, msg))
        except Exception as e:
            operator.report({'WARNING'}, "Texture load failed: %s" % e)
    else:
        tried = tex_path or "(no .pvm / .gvm found)"
        operator.report({'WARNING'}, "Texture archive not found — tried: %s" % tried)
        print("[%s] Texture archive not found — tried: %s" % (label, tried))
    return textures


# ============================================================
# PSO DC .nj model operator
# ============================================================
class IMPORT_OT_pso_nj(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_nj"
    bl_label       = "Import PSO DC .nj"
    bl_description = "Import a Phantasy Star Online Dreamcast .nj model file"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ".nj"
    filter_glob: StringProperty(default="*.nj", options={'HIDDEN'})
    xvm_filepath: StringProperty(name="Texture Archive (.pvm)",
        description="Path to .pvm archive (leave blank to auto-detect)",
        default="", subtype='FILE_PATH')
    blend_vertex_colors: BoolProperty(name="Blend Vertex Colors", default=True,
        description="Apply vertex colors as lighting in the scene")
    disable_color_correction: BoolProperty(name="Disable Color Correction", default=True,
        description="Set Color Management to Standard")
    extend_clip_distance: BoolProperty(name="Extend Viewport Clip Distance", default=True,
        description="Raise Clip End so the model is fully visible")

    def draw(self, context):
        l = self.layout
        l.label(text="Texture Archive (.pvm):"); l.prop(self, "xvm_filepath", text="")
        l.label(text="(leave blank to auto-detect)"); l.separator()
        l.prop(self, "blend_vertex_colors"); l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")

    def execute(self, context):
        filepath = self.filepath
        try:
            with open(filepath, 'rb') as f: nj_data = f.read()
        except OSError as e:
            self.report({'ERROR'}, "Cannot open: %s" % e); return {'CANCELLED'}

        textures = _make_pvm_operator_body(self, context, None, nj_data, filepath, "PSO DC .nj")

        geo = NinjaDCImporter(); geo.setTextures(textures)
        try: geo.parse(nj_data)
        except Exception as e:
            self.report({'ERROR'}, "Parse error: %s" % e); return {'CANCELLED'}

        try: mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e); return {'CANCELLED'}

        if self.extend_clip_distance: extend_clip_distance(geo)
        if self.blend_vertex_colors: disable_eevee_shadows()
        if self.disable_color_correction:
            try: context.scene.view_settings.view_transform = "Standard"
            except Exception: pass

        self.report({'INFO'}, "Imported %d mesh(es), %d tex from %s" % (
            mesh_count, len(textures), os.path.basename(filepath)))
        return {'FINISHED'}


# ============================================================
# PSO DC .rel stage operator
# ============================================================
class IMPORT_OT_pso_dc_rel(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_dc_rel"
    bl_label       = "Import PSO DC .rel"
    bl_description = "Import a Phantasy Star Online Dreamcast n.rel / d.rel stage file"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ".rel"
    filter_glob: StringProperty(default="*[nd].rel", options={'HIDDEN'})
    xvm_filepath: StringProperty(name="Texture Archive (.pvm)",
        description="Path to .pvm archive (leave blank to auto-detect)",
        default="", subtype='FILE_PATH')
    blend_vertex_colors: BoolProperty(name="Blend Vertex Colors", default=True,
        description="Apply vertex colors as lighting in the scene")
    disable_color_correction: BoolProperty(name="Disable Color Correction", default=True,
        description="Set Color Management to Standard")
    extend_clip_distance: BoolProperty(name="Extend Viewport Clip Distance", default=True,
        description="Raise Clip End so the stage is fully visible")

    def draw(self, context):
        l = self.layout
        l.label(text="Texture Archive (.pvm):"); l.prop(self, "xvm_filepath", text="")
        l.label(text="(leave blank to auto-detect)"); l.separator()
        l.prop(self, "blend_vertex_colors"); l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")

    def execute(self, context):
        filepath = self.filepath
        # Auto-locate paired file: e.g. selecting foret_01n.rel also loads foret_01d.rel
        stem = os.path.splitext(filepath)[0]
        if stem and stem[-1] in ('n', 'd'):
            base   = stem[:-1]
            d_path = base + "d.rel"
            n_path = base + "n.rel"
        else:
            d_path = n_path = filepath

        def _load(p):
            if os.path.exists(p):
                with open(p, 'rb') as f: return f.read()
            return None

        d_data = _load(d_path); n_data = _load(n_path)
        if d_data is None and n_data is None:
            self.report({'ERROR'}, "Cannot find d.rel or n.rel"); return {'CANCELLED'}

        textures = _make_pvm_operator_body(self, context, None, None, filepath, "PSO DC .rel")

        geo = NinjaDCRelImporter(); geo.setTextures(textures)
        try: geo.parse(d_data, n_data)
        except Exception as e:
            self.report({'ERROR'}, "Parse error: %s" % e); return {'CANCELLED'}

        try: mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e); return {'CANCELLED'}

        if self.extend_clip_distance: extend_clip_distance(geo)
        if self.blend_vertex_colors: disable_eevee_shadows()
        if self.disable_color_correction:
            try: context.scene.view_settings.view_transform = "Standard"
            except Exception: pass

        self.report({'INFO'}, "Imported %d mesh(es), %d tex from %s" % (
            mesh_count, len(textures), os.path.basename(filepath)))
        return {'FINISHED'}


# ============================================================
# PSO GC .gj model operator
# ============================================================
class IMPORT_OT_pso_gj(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_gj"
    bl_label       = "Import PSO GC .gj"
    bl_description = "Import a Phantasy Star Online GameCube .gj model file"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ".gj"
    filter_glob: StringProperty(default="*.gj", options={'HIDDEN'})
    xvm_filepath: StringProperty(name="Texture Archive (.gvm)",
        description="Path to .gvm archive (leave blank to auto-detect)",
        default="", subtype='FILE_PATH')
    blend_vertex_colors: BoolProperty(name="Blend Vertex Colors (Modulate 2X)", default=False,
        description="Apply vertex colors as lighting in the scene")
    disable_color_correction: BoolProperty(name="Disable Color Correction", default=True,
        description="Set Color Management to Standard")
    extend_clip_distance: BoolProperty(name="Extend Viewport Clip Distance", default=True,
        description="Raise Clip End so the model is fully visible")

    def draw(self, context):
        l = self.layout
        l.label(text="Texture Archive (.gvm):")
        l.prop(self, "xvm_filepath", text="")
        l.label(text="(leave blank to auto-detect)"); l.separator()
        l.prop(self, "blend_vertex_colors"); l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")

    def execute(self, context):
        filepath = self.filepath
        try:
            with open(filepath, 'rb') as f: gj_data = f.read()
        except OSError as e:
            self.report({'ERROR'}, "Cannot open: %s" % e); return {'CANCELLED'}

        gvm_path = (self.xvm_filepath.strip()
                    or find_compound_tex_path(filepath)
                    or find_gvm_path(filepath))
        textures = []
        if gvm_path and os.path.exists(gvm_path):
            try:
                with open(gvm_path, 'rb') as f: raw = f.read()
                textures = load_texture_archive(raw)
                msg = "Loaded %d texture(s) from %s" % (len(textures), os.path.basename(gvm_path))
                self.report({'INFO'}, msg); print("[PSO GC .gj] " + msg)
            except Exception as e:
                self.report({'WARNING'}, "Texture load failed: %s" % e)
                print("[PSO GC .gj] Texture load failed: %s" % e)
        else:
            tried = gvm_path or "(no .gj.gvm / .gvm found)"
            self.report({'WARNING'}, "GVM not found — tried: %s" % tried)
            print("[PSO GC .gj] GVM not found — tried: %s" % tried)

        geo = FlipperGCImporter(); geo.setTextures(textures)
        try: geo.parse(gj_data)
        except Exception as e:
            self.report({'ERROR'}, "Parse error: %s" % e); return {'CANCELLED'}

        try: mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e); return {'CANCELLED'}

        if self.extend_clip_distance: extend_clip_distance(geo)
        if self.blend_vertex_colors: disable_eevee_shadows()
        if self.disable_color_correction:
            try: context.scene.view_settings.view_transform = "Standard"
            except Exception: pass

        self.report({'INFO'}, "Imported %d mesh(es), %d tex from %s" % (
            mesh_count, len(textures), os.path.basename(filepath)))
        return {'FINISHED'}


# ============================================================
# PSO GC n.rel stage operator
# ============================================================
class IMPORT_OT_pso_gc_rel(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_gc_rel"
    bl_label       = "Import PSO GC Stage (.rel)"
    bl_description = "Import a Phantasy Star Online GameCube n.rel stage file"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ".rel"
    filter_glob: StringProperty(default="*n.rel", options={'HIDDEN'})
    xvm_filepath: StringProperty(name="Texture Archive (.gvm)",
        description="Path to .gvm archive (leave blank to auto-detect)",
        default="", subtype='FILE_PATH')
    blend_vertex_colors: BoolProperty(name="Blend Vertex Colors (Modulate 2X)", default=True,
        description="Apply vertex colors as lighting in the scene")
    disable_color_correction: BoolProperty(name="Disable Color Correction", default=True,
        description="Set Color Management to Standard")
    extend_clip_distance: BoolProperty(name="Extend Viewport Clip Distance", default=True,
        description="Raise Clip End so the stage is fully visible")

    def draw(self, context):
        l = self.layout
        l.label(text="Texture Archive (.gvm):")
        l.prop(self, "xvm_filepath", text="")
        l.label(text="(leave blank to auto-detect)"); l.separator()
        l.prop(self, "blend_vertex_colors"); l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")

    def execute(self, context):
        filepath = self.filepath
        try:
            with open(filepath, 'rb') as f: rel_data = f.read()
        except OSError as e:
            self.report({'ERROR'}, "Cannot open: %s" % e); return {'CANCELLED'}

        gvm_path = self.xvm_filepath.strip() or find_gvm_path(filepath)
        textures = []
        if gvm_path and os.path.exists(gvm_path):
            try:
                with open(gvm_path, 'rb') as f: raw = f.read()
                textures = gvm_load(raw)
                msg = "Loaded %d texture(s) from %s" % (len(textures), os.path.basename(gvm_path))
                self.report({'INFO'}, msg); print("[PSO GC .rel] " + msg)
            except Exception as e:
                self.report({'WARNING'}, "Texture load failed: %s" % e)
                print("[PSO GC .rel] Texture load failed: %s" % e)
        else:
            tried = gvm_path or "(no .gvm found)"
            self.report({'WARNING'}, "GVM not found — tried: %s" % tried)
            print("[PSO GC .rel] GVM not found — tried: %s" % tried)

        geo = FlipperGCImporter(); geo.setTextures(textures)
        try: geo.parse_stage(rel_data)
        except Exception as e:
            self.report({'ERROR'}, "Parse error: %s" % e); return {'CANCELLED'}

        try: mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e); return {'CANCELLED'}

        if self.extend_clip_distance: extend_clip_distance(geo)
        if self.blend_vertex_colors: disable_eevee_shadows()
        if self.disable_color_correction:
            try: context.scene.view_settings.view_transform = "Standard"
            except Exception: pass

        self.report({'INFO'}, "Imported %d mesh(es), %d tex from %s" % (
            mesh_count, len(textures), os.path.basename(filepath)))
        return {'FINISHED'}


# ============================================================
# PSO BML archive operator
# ============================================================
class IMPORT_OT_pso_bml(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_bml"
    bl_label       = "Import PSO BML Archive"
    bl_description = (
        "Import a Phantasy Star Online BML model archive. "
        "Extracts all models (.nj / .gj / .xj) and their paired textures; "
        "animations (.njm) are ignored."
    )
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".bml"
    filter_glob: StringProperty(default="*.bml", options={'HIDDEN'})

    blend_vertex_colors: BoolProperty(
        name="Blend Vertex Colors",
        description="Apply vertex colors as lighting in the scene",
        default=True,
    )
    disable_color_correction: BoolProperty(
        name="Disable Color Correction",
        description="Set Color Management to Standard",
        default=True,
    )
    extend_clip_distance: BoolProperty(
        name="Extend Viewport Clip Distance",
        description="Raise Clip End so imported models are fully visible",
        default=True,
    )

    def draw(self, context):
        l = self.layout
        l.prop(self, "blend_vertex_colors")
        l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")

    def execute(self, context):
        filepath = self.filepath
        try:
            with open(filepath, 'rb') as f:
                bml_data = f.read()
        except OSError as e:
            self.report({'ERROR'}, "Cannot open: %s" % e)
            return {'CANCELLED'}

        # Extract all entries from the BML
        try:
            entries = bml_read(bml_data)
        except Exception as e:
            self.report({'ERROR'}, "BML parse error: %s" % e)
            return {'CANCELLED'}

        if not entries:
            self.report({'WARNING'}, "No entries found in BML")
            return {'CANCELLED'}

        # Build (model_entry, texture_entry_or_None) pairs.
        # Priority order for finding a texture for a given model entry:
        #   1. The BML entry immediately following the model (bml_read standard layout)
        #   2. A BML entry whose name is the model filename + a texture extension
        #      (compound extension, e.g. "robby_cat.nj.xvm" for "robby_cat.nj")
        _COMPOUND_TEX_EXTS = ('.xvm', '.gvm', '.pvm')
        # Build a lookup: lower-case filename -> entry, for compound-ext search
        _entry_by_name = {e['filename'].lower(): e for e in entries}

        pairs = []
        i = 0
        while i < len(entries):
            name = entries[i]['filename']
            ext  = os.path.splitext(name)[1].lower()
            if ext in _MODEL_EXTS:
                model = entries[i]
                tex   = None
                # 1. Check immediate next entry (standard embedded layout)
                if (i + 1 < len(entries) and
                        os.path.splitext(entries[i + 1]['filename'])[1].lower()
                        in _TEXTURE_EXTS):
                    tex = entries[i + 1]
                    i  += 1          # consume the texture entry
                # 2. Fall back to compound-named entry anywhere in the archive
                if tex is None:
                    for tex_ext in _COMPOUND_TEX_EXTS:
                        compound = (name + tex_ext).lower()
                        if compound in _entry_by_name:
                            tex = _entry_by_name[compound]
                            break
                pairs.append((model, tex))
            # .njm / .gjm and anything else is silently skipped
            i += 1

        if not pairs:
            self.report({'WARNING'}, "No importable models (.nj/.gj/.xj) found in BML")
            return {'CANCELLED'}

        # Try to load a sidecar texture archive that lives beside the .bml
        # (same base name, extension .gvm / .pvm / .xvm).  Used as a fallback
        # for models whose BML entry carries no embedded texture.
        sidecar_textures = []
        bml_stem = os.path.splitext(filepath)[0]
        for tex_ext in ('.gvm', '.GVM', '.pvm', '.PVM', '.xvm', '.XVM'):
            candidate = bml_stem + tex_ext
            if os.path.exists(candidate):
                try:
                    with open(candidate, 'rb') as f:
                        raw = f.read()
                    sidecar_textures = load_texture_archive(raw)
                    msg = "Loaded %d sidecar texture(s) from %s" % (
                        len(sidecar_textures), os.path.basename(candidate))
                    self.report({'INFO'}, msg)
                    print("[PSO BML] " + msg)
                except Exception as e:
                    self.report({'WARNING'}, "Sidecar texture load failed (%s): %s" % (
                        os.path.basename(candidate), e))
                break   # stop after the first match

        total_meshes = 0
        total_tex    = 0
        combined_geo = None   # used for clip-distance calculation

        for model_entry, tex_entry in pairs:
            name = model_entry['filename']
            ext  = os.path.splitext(name)[1].lower()

            # Load textures — four sources tried in priority order:
            textures = []

            # 1. Embedded archive inside the BML
            if tex_entry and tex_entry['data']:
                try:
                    textures = load_texture_archive(tex_entry['data'])
                except Exception as e:
                    self.report({'WARNING'}, "Texture load failed for %s: %s" % (
                        tex_entry['filename'], e))

            # 2. Per-model external file in the BML's directory
            #    (e.g. robby_cat.GVM beside the .bml for robby_cat.nj)
            if not textures:
                model_stem = os.path.splitext(name)[0]
                bml_dir    = os.path.dirname(filepath)
                for tex_ext in ('.gvm', '.GVM', '.pvm', '.PVM', '.xvm', '.XVM'):
                    candidate = os.path.join(bml_dir, model_stem + tex_ext)
                    if os.path.exists(candidate):
                        try:
                            with open(candidate, 'rb') as f:
                                raw = f.read()
                            textures = load_texture_archive(raw)
                            print("[PSO BML] Loaded %d texture(s) from %s" % (
                                len(textures), os.path.basename(candidate)))
                        except Exception as e:
                            self.report({'WARNING'}, "Per-model texture load failed (%s): %s" % (
                                os.path.basename(candidate), e))
                        break

            # 3. BML-level sidecar (e.g. biri_ball.GVM shared across all models)
            if not textures and sidecar_textures:
                textures = sidecar_textures

            # Pick the right importer for the model format.
            if ext == '.nj':
                geo = NinjaDCImporter()
            elif ext == '.gj':
                geo = FlipperGCImporter()
            elif ext == '.xj':
                geo = NinjaXJImporter()
            else:
                continue

            geo.setTextures(textures)
            try:
                geo.parse(model_entry['data'])
            except Exception:
                # DC parsing failed — some .nj files in BB BML archives use the
                # XJ mesh layout internally despite the .nj extension.  Retry.
                if ext == '.nj':
                    geo = NinjaXJImporter()
                    geo.setTextures(textures)
                    try:
                        geo.parse(model_entry['data'])
                    except Exception as e2:
                        self.report({'WARNING'}, "Parse error for %s: %s" % (name, e2))
                        continue
                else:
                    self.report({'WARNING'}, "Parse error for %s" % name)
                    continue

            if not geo.meshes_data:
                continue

            try:
                mc = build_blender_scene(geo, name, self.blend_vertex_colors)
            except Exception as e:
                self.report({'WARNING'}, "Scene build error for %s: %s" % (name, e))
                continue

            total_meshes += mc
            total_tex    += len(textures)
            combined_geo  = geo   # keep last for clip-distance (all share world space)

        if combined_geo and self.extend_clip_distance:
            extend_clip_distance(combined_geo)
        if self.blend_vertex_colors:
            disable_eevee_shadows()
        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = "Standard"
            except Exception:
                pass

        msg = "Imported %d mesh(es), %d texture(s) from %s (%d model(s))" % (
            total_meshes, total_tex, os.path.basename(filepath), len(pairs))
        self.report({'INFO'}, msg)
        print("[PSO BML] " + msg)
        return {'FINISHED'}


# ============================================================
# Menu hooks
# ============================================================
def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_pso_rel.bl_idname,    text="PSO BB Ninja Stage (.rel)")
    self.layout.operator(IMPORT_OT_pso_xj.bl_idname,     text="PSO BB Ninja Model (.xj)")
    self.layout.operator(IMPORT_OT_pso_nj.bl_idname,     text="PSO DC Ninja Model (.nj)")
    self.layout.operator(IMPORT_OT_pso_dc_rel.bl_idname, text="PSO DC Ninja Stage (.rel)")
    self.layout.operator(IMPORT_OT_pso_gj.bl_idname,     text="PSO GC Flipper Model (.gj)")
    self.layout.operator(IMPORT_OT_pso_gc_rel.bl_idname, text="PSO GC Flipper Stage (.rel)")
    self.layout.operator(IMPORT_OT_pso_bml.bl_idname,    text="PSO BML Archive (.bml)")

# ============================================================
# Registration
# ============================================================
_CLASSES = (
    IMPORT_OT_pso_rel,
    IMPORT_OT_pso_xj,
    IMPORT_OT_pso_nj,
    IMPORT_OT_pso_dc_rel,
    IMPORT_OT_pso_gj,
    IMPORT_OT_pso_gc_rel,
    IMPORT_OT_pso_bml,
)

def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)

if __name__ == "__main__":
    register()
