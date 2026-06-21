bl_info = {
    "name": "PSO Ultimate Importer",
    "author": "Theanine3D",
    "version": (1, 1, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > PSO …",
    "description": (
        "Import Phantasy Star Online model and stage files"
    ),
    "category": "Import-Export",
}

import bpy
import math
import struct
import os
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, EnumProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup

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

# Per-vertex byte sizes for each NJD_CV_* chunk type (ch 32-50).
# Formula: 12 (pos) + 4 (SH padding) + 4 (packed-normal for VNX) or 12 (float normal)
#           + 4 (SH normal padding) + 4 (color for D8) + 4 (nofs+pad for NF).
# Used by GC readChunks disambiguation to validate candidate vertex-chunk lengths.
_GC_VERTEX_SIZE = {
    32: 16,   # NJD_CV_SH      pos(12)+w(4)
    33: 32,   # NJD_CV_VN_SH   pos(12)+w(4)+norm(12)+w(4)
    34: 12,   # NJD_CV
    35: 16,   # NJD_CV_D8      pos(12)+color(4)
    36: 12,   # NJD_CV_UF
    37: 12,   # NJD_CV_NF
    38: 12,   # NJD_CV_S5
    39: 12,   # NJD_CV_S4
    40: 12,   # NJD_CV_IN
    41: 24,   # NJD_CV_VN      pos(12)+norm(12)
    42: 28,   # NJD_CV_VN_D8   pos(12)+norm(12)+color(4)
    43: 24,   # NJD_CV_VN_UF   pos(12)+norm(12)
    44: 28,   # NJD_CV_VN_NF   pos(12)+norm(12)+nofs(2)+pad(2)
    45: 24,   # NJD_CV_VN_S5
    46: 24,   # NJD_CV_VN_S4
    47: 24,   # NJD_CV_VN_IN
    48: 16,   # NJD_CV_VNX     pos(12)+packed-norm(4)
    49: 20,   # NJD_CV_VNX_D8  pos(12)+packed-norm(4)+color(4)
    50: 16,   # NJD_CV_VNX_UF  pos(12)+packed-norm(4)
}

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

    # For non-VQ mip formats the data is stored smallest-mip-first; skip past
    # the smaller levels to reach the full-size pixel data.
    if data_fmt in HAS_MIPS and data_fmt not in VQ:
        pos += _pvr_mipmap_skip(width, height, False)

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
        # For VQ+Mip the codebook comes first, then small mip index arrays, then
        # the full-size index.  Skip the small mip indices now that the codebook
        # has been read.
        if data_fmt in HAS_MIPS:
            pos += _pvr_mipmap_skip(width, height, True)
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
            'positions':  pos_list,
            'normals':    norm_list,
            'colors':     color_list,
            'uvs':        uv_list,
            'triangles':  tri_list,
            'mat_index':  mat_index,
            'bone_index': getattr(self, 'current_bone_index', -1),
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
    mesh_objects  = []   # list of (obj, bone_indices_list) for armature setup

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
        mesh_objects.append((obj, md.get('bone_indices', [])))

    # --- Armature (only if the importer tracked a node hierarchy) ---
    geo_nodes = getattr(geo, 'nodes', None)
    if geo_nodes:
        arm_data = bpy.data.armatures.new(col_name)
        arm_obj  = bpy.data.objects.new(col_name + "_Armature", arm_data)
        arm_obj.rotation_euler = (math.radians(90), 0, 0)
        arm_obj.show_in_front  = True
        collection.objects.link(arm_obj)

        saved_active = bpy.context.view_layer.objects.active
        bpy.context.view_layer.objects.active = arm_obj
        bpy.ops.object.mode_set(mode='EDIT')

        edit_bones = arm_data.edit_bones
        bone_list  = []   # indexed by node DFS order
        for ni, node_info in enumerate(geo_nodes):
            eb   = edit_bones.new("bone_%03d" % ni)
            wp   = node_info['world_pos']
            eb.head = (wp[0], wp[1], wp[2])
            axes = node_info.get('world_axes')
            if axes:
                # Point bone in NJ world-Y direction; align roll to NJ world-Z
                y_ax = axes[1]; z_ax = axes[2]
                bl = 0.05
                eb.tail = (wp[0] + y_ax[0]*bl, wp[1] + y_ax[1]*bl, wp[2] + y_ax[2]*bl)
                try:
                    from mathutils import Vector
                    eb.align_roll(Vector(z_ax))
                except Exception:
                    pass
            else:
                eb.tail = (wp[0], wp[1] + 0.05, wp[2])
            pi = node_info['parent_index']
            if 0 <= pi < len(bone_list):
                eb.parent        = bone_list[pi]
                eb.use_connect   = False
            bone_list.append(eb)

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.context.view_layer.objects.active = saved_active

        # NJ DashMat4.rotate(x,y,z) applies Rx*Ry*Rz in row-vector convention
        # = extrinsic X→Y→Z = intrinsic ZYX.  Blender's 'ZYX' mode matches.
        for pb in arm_obj.pose.bones:
            pb.rotation_mode = 'ZYX'

        # Populate rest-transform cache for NJM animation import
        rt = {}
        for ni, node_info in enumerate(geo_nodes):
            rt[ni] = {
                'pos': node_info.get('local_pos', (0.0, 0.0, 0.0)),
                'rot': node_info.get('local_rot', (0.0, 0.0, 0.0)),
            }
        bone_count = len(geo_nodes)
        _pso_rest_transforms[bone_count] = rt
        # Strip directory, extension, and "_Armature" suffix to get a plain stem for name matching
        import os as _os
        _stem = _os.path.splitext(_os.path.basename(col_name))[0]
        _pso_armatures.append((_stem, bone_count, arm_obj))

        # Assign per-vertex bone groups, armature modifier, and parent each mesh to the armature
        n_nodes = len(geo_nodes)
        for obj, bone_indices in mesh_objects:
            if bone_indices:
                # Group vertex positions by which bone they belong to
                bone_to_verts = {}
                for vi, bi in enumerate(bone_indices):
                    if 0 <= bi < n_nodes:
                        bone_to_verts.setdefault(bi, []).append(vi)
                for bi, vert_indices in bone_to_verts.items():
                    vg = obj.vertex_groups.new(name="bone_%03d" % bi)
                    vg.add(vert_indices, 1.0, 'REPLACE')
            mod        = obj.modifiers.new(name="Armature", type='ARMATURE')
            mod.object = arm_obj
            obj.parent = arm_obj
            obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()

    return len(geo.meshes_data)

# ============================================================
# Ninja XJ importer (props / character models)
# Produces the same meshes_data / materials_data / textures
# structure as NinjaStageGeometry so build_blender_scene reuses it.
# ============================================================
class NinjaXJImporter:

    def __init__(self):
        self.texNames            = []   # filename strings from NJTL chunk
        self.vertex_stack        = {}
        self.materials_data      = []
        self.meshes_data         = []
        self.textures            = []   # from .xvm
        self.current_matrix      = DashMat4()
        self.material            = {}
        self.nodes               = []   # node hierarchy for armature: {parent_index, world_pos, flags}
        self.current_bone_index  = -1   # set before each readMesh() call

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
    def readBone(self, pMatrix=None, parent_idx=-1):
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

        # Record this node in DFS order for armature building
        my_idx = len(self.nodes)
        m = mat.mtx
        self.nodes.append({
            'parent_index': parent_idx,
            'world_pos':    (m[3][0], m[3][1], m[3][2]),
            'world_axes':   ((m[0][0], m[0][1], m[0][2]),
                             (m[1][0], m[1][1], m[1][2]),
                             (m[2][0], m[2][1], m[2][2])),
            'local_pos':    node['pos'] if not (node['flags'] & 0x01) else (0.0, 0.0, 0.0),
            'local_rot':    node['rot'] if not (node['flags'] & 0x02) else (0.0, 0.0, 0.0),
            'flags':        node['flags'],
        })
        self.current_bone_index = my_idx

        size = self.bs.getSize()
        if node['meshOfs'] >= size or node['childOfs'] >= size or node['siblingOfs'] >= size:
            return

        if node['meshOfs'] != 0:
            self.bs.seek(node['meshOfs'])
            self.readMesh()

        if node['childOfs'] != 0:
            self.bs.seek(node['childOfs'])
            self.readBone(mat, my_idx)

        if node['siblingOfs'] != 0:
            self.bs.seek(node['siblingOfs'])
            self.readBone(pMatrix, parent_idx)

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

            vertex['bone_index'] = self.current_bone_index
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

        bone_index_list = []

        for point in triangles:
            if point not in self.vertex_stack:
                continue
            vert = self.vertex_stack[point]
            tri_list.append(len(pos_list))
            pos_list.append(vert['pos'])
            bone_index_list.append(vert.get('bone_index', -1))
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
            'positions':    pos_list,
            'normals':      norm_list,
            'colors':       color_list,
            'uvs':          uv_list,
            'triangles':    tri_list,
            'mat_index':    mat_index,
            'bone_indices': bone_index_list,
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
                # "No-length" chunk types (NJD_CN/CE, BITS, TINY, STRIP, VOLUME, MATERIAL):
                #   first BE uint16 = ch_cf word  (ch in low byte, cf in high byte)
                #   the individual handlers then read a length word on their own.
                # "With-length" chunk types (VERTEX only):
                #   first BE uint16  = length word
                #   second BE uint16 = ch_cf word  (ch in low byte, cf in high byte)
                #
                # Disambiguation problem: a vertex chunk's length word has its LOW BYTE
                # equal to (4 + vcount*vsz) / 4, which can land in the TINY (8-9, 16-31)
                # or BITS (1-5) range for small vcount values.  When that happens the
                # heuristic below would misidentify the length word as a no-len ch_cf.
                # Guard against this by peeking at the NEXT word whenever ch_cand falls
                # in CHUNK_TINY or CHUNK_BITS: if that word's low byte is a VERTEX type
                # we know word0 was actually the length of a vertex chunk.
                word0 = bs.readUShort()
                ch_cand = word0 & 0xFF
                no_len = (ch_cand == 0 or ch_cand == 0xFF or
                          ch_cand in CHUNK_BITS or
                          ch_cand in CHUNK_TINY or
                          ch_cand in CHUNK_STRIP or
                          ch_cand in CHUNK_VOLUME or
                          0x10 <= ch_cand <= 0x1F)
                if no_len and ch_cand not in (0, 0xFF):
                    # Disambiguation: in GC format every vertex chunk is preceded
                    # by a length word whose low byte can coincide with any no-len
                    # chunk type (TINY=8-9, BITS=1-5, STRIP=64-75, VOLUME=56-58,
                    # MATERIAL=17-23, etc.).  Peek at the next word: if its low
                    # byte is a VERTEX chunk type (32-50) then word0 was the
                    # vertex-chunk length, not a no-len ch_cf.
                    if bs.pos + 2 <= bs.getSize():
                        peek_pos = bs.pos
                        word1_peek = bs.readUShort()
                        vc_ch = word1_peek & 0xFF
                        if vc_ch in CHUNK_VERTEX:
                            # Candidate: word0 is the vertex-chunk length, word1 is ch_cf.
                            # Validate with two conditions:
                            #  1. (word0*4 - 4) must be divisible by the per-vertex size.
                            #  2. The expected vcount must equal the actual vcount word in
                            #     the stream (immediately after word1).
                            # Together these reject false positives where a STRIP ch_cf is
                            # followed by a clen whose low byte falls in the VERTEX range
                            # (e.g. NJD_CS_UVN with clen=288 whose low byte is 0x20=32).
                            vsz = _GC_VERTEX_SIZE.get(vc_ch, 12)
                            body_bytes = word0 * 4 - 4   # bytes of data after vcount+vofs
                            is_vertex = False
                            if body_bytes > 0 and body_bytes % vsz == 0:
                                exp_vcount = body_bytes // vsz
                                if bs.pos + 2 <= bs.getSize():
                                    vcount_pos = bs.pos          # position of vcount word
                                    actual_vcount = bs.readUShort()
                                    if actual_vcount == exp_vcount:
                                        # Confirmed vertex chunk.
                                        # Restore to vcount_pos so _vChunk can read it.
                                        bs.seek(vcount_pos)
                                        is_vertex = True
                                    else:
                                        # Mismatch → false positive.
                                        bs.seek(peek_pos)
                            if is_vertex:
                                # word0 was the vertex-chunk length; word1 is the ch_cf.
                                # bs is positioned at vcount — _vChunk will read from here.
                                no_len = False
                                ch = vc_ch
                                cf = (word1_peek >> 8) & 0xFF
                            else:
                                # False positive — word0 really was the no-len ch_cf
                                bs.seek(peek_pos)
                                ch = ch_cand
                                cf = (word0 >> 8) & 0xFF
                        else:
                            bs.seek(peek_pos)   # restore — word0 really was ch_cf
                            ch = ch_cand
                            cf = (word0 >> 8) & 0xFF
                    else:
                        ch = ch_cand
                        cf = (word0 >> 8) & 0xFF
                elif no_len:
                    # ch_cand is 0 (NULL) or 0xFF (END) — unambiguous
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

            v['bone_index'] = getattr(self, 'current_bone_index', -1)

            if ch == NJD_CV_VN_NF:
                # NJD_CV_VN_NF has a 4-byte field: [nofs (int16), padding (int16)].
                # In GC big-endian NJ, the 4-byte word swap (each uint32 is byte-reversed
                # relative to LE) swaps the two 16-bit halves, so the layout becomes
                # [padding, nofs] instead of [nofs, padding].
                if bs._e == '>':
                    bs.readShort()           # padding (skip)
                    nofs = bs.readShort()    # actual nofs
                else:
                    nofs = bs.readShort()    # nofs
                    bs.readShort()           # padding (skip)
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
        pos_list=[]; norm_list=[]; color_list=[]; uv_list=[]; tri_list=[]; bone_index_list=[]
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
                bone_index_list.append(vt.get('bone_index', -1))
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
            'positions':    pos_list,
            'normals':      norm_list,
            'colors':       color_list,
            'uvs':          uv_list,
            'triangles':    tri_list,
            'mat_index':    mi,
            'bone_indices': bone_index_list,
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
    best_B_validated = False
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
            best_B_validated = True
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

    if not best_B_validated:
        # Base found only via relaxed (no bone-structure) fallback — not
        # trustworthy enough to patch; skip to avoid corrupting strip data.
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
        self.texNames            = []
        self.vertex_stack        = {}
        self.materials_data      = []
        self.meshes_data         = []
        self.textures            = []
        self.current_matrix      = DashMat4()
        self.material            = {}
        self.store_ofs           = [None] * 256
        self.jump_to             = 0
        self.nodes               = []   # node hierarchy for armature
        self.current_bone_index  = -1

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
        #
        # Some files contain TWO POF0 chunks: a small one before the NJCM that
        # relocates NJTL pointers, and a larger one AFTER the NJCM that relocates
        # the bone/mesh pointers inside NJCM.  Always prefer the POF0 whose
        # payload starts after the NJCM ends; fall back to the first one if no
        # such chunk exists.
        pof0_chunks = chunk_map.get(MAGIC_POF0, [])

        for off, clen in chunk_map.get(MAGIC_NJCM, []):
            njcm_bytes = data[off : off + clen]
            if pof0_chunks:
                # Before-NJCM POF0 chunks carry NJTL texture-list pointers, not
                # NJCM bone/mesh pointers — never apply them to the NJCM payload.
                # Only use a POF0 that appears after the NJCM in the file, and
                # only when the root bone's pointer fields are not already valid
                # (i.e. the file has a non-zero serialization base, as with GC
                # boss models like the gryphon).  DC/BB models whose NJCM pointers
                # are already in-range are left untouched.
                bo_str = '>' if big_endian else '<'
                root_needs_reloc = True
                if len(njcm_bytes) >= 52:
                    fl = struct.unpack_from(bo_str + 'I', njcm_bytes, 0)[0]
                    sz = len(njcm_bytes)
                    root_needs_reloc = (
                        fl > 0x3FFF or
                        any(struct.unpack_from(bo_str + 'I', njcm_bytes, fo)[0] not in (0,) and
                            struct.unpack_from(bo_str + 'I', njcm_bytes, fo)[0] >= sz
                            for fo in (4, 44, 48))
                    )
                if root_needs_reloc:
                    njcm_end = off + clen
                    after = [(po, pc) for po, pc in pof0_chunks if po > njcm_end]
                    if after:
                        pof0_off, pof0_clen = after[0]
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

    def _readBone(self, pMatrix=None, parent_idx=-1):
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

        my_idx = len(self.nodes)
        m = mat.mtx
        # For quaternion-rotation bones, derive an euler approximation for rest_transforms
        if flags & 0x400:
            import math as _m
            qx, qy, qz, qw = node['rot'][0], node['rot'][1], node['rot'][2], node['w']
            _local_rot = (
                _m.atan2(2*(qw*qx + qy*qz), 1 - 2*(qx*qx + qy*qy)),
                _m.asin(max(-1.0, min(1.0, 2*(qw*qy - qz*qx)))),
                _m.atan2(2*(qw*qz + qx*qy), 1 - 2*(qy*qy + qz*qz)),
            )
        else:
            _local_rot = node['rot'] if not (flags & 0x02) else (0.0, 0.0, 0.0)
        self.nodes.append({
            'parent_index': parent_idx,
            'world_pos':    (m[3][0], m[3][1], m[3][2]),
            'world_axes':   ((m[0][0], m[0][1], m[0][2]),
                             (m[1][0], m[1][1], m[1][2]),
                             (m[2][0], m[2][1], m[2][2])),
            'local_pos':    node['pos'] if not (flags & 0x01) else (0.0, 0.0, 0.0),
            'local_rot':    _local_rot,
            'flags':        flags,
        })
        self.current_bone_index = my_idx

        sz = self.bs.getSize()
        bail = node['meshOfs'] >= sz or node['childOfs'] >= sz or node['siblingOfs'] >= sz
        # Combined out-of-bounds bail: if any pointer is invalid, skip this
        # node's mesh and its entire subtree.  This matches the traversal
        # behaviour of the reference importer and is required to suppress a
        # specific early-DP mis-fire on certain character models (e.g. NiGHTS).
        if bail:
            return
        if node['meshOfs'] != 0:
            self.bs.seek(node['meshOfs'])
            self._readMesh()
        if node['childOfs'] != 0:
            self.bs.seek(node['childOfs'])
            self._readBone(mat, my_idx)
        if node['siblingOfs'] != 0:
            self.bs.seek(node['siblingOfs'])
            self._readBone(pMatrix, parent_idx)

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
def find_tex_archive(filepath, platform='BB'):
    """Locate a texture archive (.xvm / .pvm / .gvm) for a model or stage file.

    Builds a list of stem candidates by stripping known suffixes from the
    filename, then tries each archive extension in a platform-priority order:
        BB  →  .xvm, .pvm, .gvm
        DC  →  .pvm, .xvm, .gvm
        GC  →  .gvm, .xvm, .pvm

    Name-based matches are tried first (all candidates × all extensions in
    priority order), then a single-archive-in-directory fallback.

    Stem candidate logic covers:
      - Bare stem:              model.nj  →  model
      - Trailing type letter:   forest01n.rel  →  forest01  (n/d/c/r stripped)
      - Underscore+digit suffix: forest_01  →  forest  (BB stage convention)
    """
    stem      = os.path.splitext(filepath)[0]
    directory = os.path.dirname(filepath)

    # Build ordered list of name stems to try, most-specific first.
    stems = []
    if stem and stem[-1].lower() in ('n', 'd', 'c', 'r'):
        base = stem[:-1]
        stems.append(base)
        # BB stage convention: strip underscore+digit suffix of length 1–3
        for strip in range(1, 4):
            if len(base) >= strip and base[-strip] == '_':
                stems.append(base[:-strip])
    stems.append(stem)

    # Extension priority order by platform
    if platform == 'GC':
        exts = ('.gvm', '.xvm', '.pvm')
    elif platform == 'DC':
        exts = ('.pvm', '.xvm', '.gvm')
    else:  # BB (default)
        exts = ('.xvm', '.pvm', '.gvm')

    # Name-based search: for each extension in priority order, try every stem
    for ext in exts:
        for s in stems:
            p = s + ext
            if os.path.exists(p):
                return p

    # Directory fallback: return a lone archive of the highest-priority type
    try:
        dir_files = os.listdir(directory)
    except OSError:
        dir_files = []
    for ext in exts:
        matches = [f for f in dir_files if f.lower().endswith(ext)]
        if len(matches) == 1:
            return os.path.join(directory, matches[0])

    return None


def find_skybox_files(rel_filepath):
    """Return (skybox_model_path, skybox_tex_path) for a given n.rel filepath.

    Replaces the trailing 'n.rel' with 's.xj', 's.nj', or 's.gj' (tried in that
    order). For the matching model, also checks for a companion texture archive
    by trying '.xvm', '.pvm', '.gvm' (again in order).
    Returns (None, None) if no skybox model is found.
    """
    name = os.path.basename(rel_filepath)
    if not name.lower().endswith('n.rel'):
        return None, None
    # base stem without the trailing letter+ext (e.g. "MAP_FOREST01")
    base_stem = name[:-5]   # strip "n.rel" / "N.REL" (5 chars regardless of case)
    folder    = os.path.dirname(rel_filepath)

    try:
        dir_files = os.listdir(folder)
    except OSError:
        return None, None

    # Case-insensitive scan for the skybox model (base + 's' + model ext)
    sky_model_path = None
    for entry in dir_files:
        entry_stem, entry_ext = os.path.splitext(entry)
        if (entry_stem.lower() == (base_stem + 's').lower()
                and entry_ext.lower() in ('.xj', '.nj', '.gj')):
            sky_model_path = os.path.join(folder, entry)
            break

    if sky_model_path is None:
        return None, None

    # Case-insensitive scan for the companion texture archive
    sky_tex_path = None
    for entry in dir_files:
        entry_stem, entry_ext = os.path.splitext(entry)
        if (entry_stem.lower() == (base_stem + 's').lower()
                and entry_ext.lower() in ('.xvm', '.pvm', '.gvm')):
            sky_tex_path = os.path.join(folder, entry)
            break

    return sky_model_path, sky_tex_path


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
# Shared skybox import helper
# ============================================================
def _import_skybox(operator, rel_filepath, blend_vertex_colors, log_prefix):
    """Attempt to find and import a skybox model beside the given n.rel file.

    Reports INFO/WARNING through the operator.  Selects the correct geometry
    parser based on the skybox file extension (.xj / .nj / .gj).
    """
    sky_model_path, sky_tex_path = find_skybox_files(rel_filepath)
    if sky_model_path is None:
        print("%s No skybox file found alongside %s" % (log_prefix, os.path.basename(rel_filepath)))
        return None

    # Load skybox textures
    sky_textures = []
    if sky_tex_path:
        try:
            with open(sky_tex_path, 'rb') as f:
                raw = f.read()
            sky_textures = load_texture_archive(raw)
            msg = "Loaded %d skybox texture(s) from %s" % (
                len(sky_textures), os.path.basename(sky_tex_path))
            operator.report({'INFO'}, msg)
            print("%s %s" % (log_prefix, msg))
        except Exception as e:
            operator.report({'WARNING'}, "Skybox texture load failed: %s" % e)
            print("%s Skybox texture load failed: %s" % (log_prefix, e))

    # Load skybox model data
    try:
        with open(sky_model_path, 'rb') as f:
            sky_data = f.read()
    except OSError as e:
        operator.report({'WARNING'}, "Cannot open skybox: %s" % e)
        return None

    # Pick the right importer
    ext = os.path.splitext(sky_model_path)[1].lower()
    if ext == '.xj':
        sky_geo = NinjaXJImporter()
    elif ext == '.nj':
        sky_geo = NinjaDCImporter()
    else:   # .gj
        sky_geo = FlipperGCImporter()

    sky_geo.setTextures(sky_textures)
    try:
        sky_geo.parse(sky_data)
    except Exception as e:
        operator.report({'WARNING'}, "Skybox parse error: %s" % e)
        print("%s Skybox parse error: %s" % (log_prefix, e))
        return None

    if not sky_geo.meshes_data:
        operator.report({'WARNING'}, "Skybox parsed but contained no meshes")
        return None

    try:
        sky_mesh_count = build_blender_scene(sky_geo, sky_model_path, blend_vertex_colors)
    except Exception as e:
        operator.report({'WARNING'}, "Skybox scene build error: %s" % e)
        print("%s Skybox scene build error: %s" % (log_prefix, e))
        return None

    msg = "Imported skybox: %d mesh(es) from %s" % (
        sky_mesh_count, os.path.basename(sky_model_path))
    operator.report({'INFO'}, msg)
    print("%s %s" % (log_prefix, msg))
    return sky_geo


_PLATFORM_ITEMS = [
    ('AUTO', "Autodetect",      "Automatically detect the platform from the file extension and contents"),
    ('BB',   "Blue Burst (PC)", "Phantasy Star Online Blue Burst (.xj / n.rel / .xvm)"),
    ('DC',   "Dreamcast (DC)",  "Phantasy Star Online v2 (.nj / n.rel / .pvm)"),
    ('GC',   "GameCube (GC)",   "Phantasy Star Online Episode I, II, and III (.gj / n.rel / .gvm)"),
]


def detect_platform(filepath):
    """Infer the PSO platform from a model/stage filepath.

    Strategy:
      - Actor files (.xj/.nj/.gj): extension is unambiguous.
      - Stage files (.rel): check for a DC sibling (paired d.rel/n.rel), then
        scan the file for GC-specific magic bytes; default to BB.
    Returns 'BB', 'DC', or 'GC'.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.xj':
        return 'BB'
    if ext == '.gj':
        return 'GC'
    if ext == '.nj':
        return 'DC'

    # .rel — check for DC's characteristic paired d.rel / n.rel sibling.
    # Use a case-insensitive directory scan so filenames like MAP_LOBBY00N.REL
    # are matched regardless of case.
    name_stem = os.path.splitext(os.path.basename(filepath))[0]
    if name_stem and name_stem[-1].lower() in ('n', 'd'):
        base         = name_stem[:-1].lower()
        other_letter = 'd' if name_stem[-1].lower() == 'n' else 'n'
        folder       = os.path.dirname(filepath)
        try:
            for entry in os.listdir(folder):
                entry_stem, entry_ext = os.path.splitext(entry)
                if (entry_ext.lower() == '.rel'
                        and entry_stem.lower() == base + other_letter):
                    return 'DC'
        except OSError:
            pass

    # Scan for GC geometry/texture-list magic bytes
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
        if b'GJCM' in data or b'GJTL' in data:
            return 'GC'
    except OSError:
        pass

    return 'BB'

# ============================================================
# PSO Actor Model operator  (.xj / .nj / .gj)
# ============================================================
class IMPORT_OT_pso_actor(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_actor"
    bl_label       = "PSO Actor Model"
    bl_description = "Import a Phantasy Star Online character or prop model (.xj / .nj / .gj)"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ""
    filter_glob: StringProperty(default="*.xj;*.nj;*.gj", options={'HIDDEN'})

    platform: EnumProperty(
        name="Platform",
        description="Which version of PSO the file comes from",
        items=_PLATFORM_ITEMS,
        default='AUTO',
    )
    xvm_filepath: StringProperty(
        name="Texture Archive",
        description="Texture archive in the same folder (.xvm / .pvm / .gvm). Leave blank to auto-detect",
        default="",
    )
    blend_vertex_colors: BoolProperty(
        name="Blend Vertex Colors",
        description="Apply vertex colors as lighting in the scene",
        default=True,
    )
    disable_color_correction: BoolProperty(
        name="Disable Color Correction",
        description="Set Color Management to Standard so textures match the original game's appearance",
        default=True,
    )
    extend_clip_distance: BoolProperty(
        name="Extend Viewport Clip Distance",
        description="Raise Clip End so the imported model is fully visible",
        default=True,
    )
    import_animations: BoolProperty(
        name="Import Animations",
        description="Search the model's directory for .njm animation files and import them as Blender Actions (only for .nj / .xj models)",
        default=True,
    )

    def draw(self, context):
        l = self.layout
        l.prop(self, "platform")
        l.separator()
        if self.platform == 'AUTO':
            ext_hint = '.xvm / .pvm / .gvm'
        else:
            ext_hint = {'BB': '.xvm', 'DC': '.pvm', 'GC': '.gvm'}.get(self.platform, '')
        l.label(text="Texture Archive (%s):" % ext_hint)
        l.prop(self, "xvm_filepath", text="")
        l.label(text="(leave blank to auto-detect)")
        l.separator()
        l.prop(self, "blend_vertex_colors")
        l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")
        l.prop(self, "import_animations")

    def execute(self, context):
        filepath = self.filepath
        try:
            with open(filepath, 'rb') as f:
                model_data = f.read()
        except OSError as e:
            self.report({'ERROR'}, "Cannot open: %s" % e)
            return {'CANCELLED'}

        platform = self.platform
        if platform == 'AUTO':
            platform = detect_platform(filepath)
            self.report({'INFO'}, "Autodetected platform: %s" % platform)
        label = "PSO %s Actor" % platform

        # ── Resolve texture archive ──────────────────────────────────────────
        manual_name = self.xvm_filepath.strip()
        textures = []
        tex_path = (os.path.join(os.path.dirname(filepath), manual_name) if manual_name
                    else find_compound_tex_path(filepath) or find_tex_archive(filepath, platform))

        if tex_path and os.path.exists(tex_path):
            try:
                with open(tex_path, 'rb') as f:
                    raw = f.read()
                textures = load_texture_archive(raw)
                msg = "Loaded %d texture(s) from %s" % (len(textures), os.path.basename(tex_path))
                self.report({'INFO'}, msg)
                print("[%s] %s" % (label, msg))
            except Exception as e:
                self.report({'WARNING'}, "Texture load failed: %s" % e)
        else:
            tried = tex_path or "(no texture archive found)"
            self.report({'WARNING'}, "Texture archive not found — tried: %s" % tried)
            print("[%s] Texture archive not found — tried: %s" % (label, tried))

        # ── Parse geometry ───────────────────────────────────────────────────
        if platform == 'BB':
            geo = NinjaXJImporter()
        elif platform == 'DC':
            geo = NinjaDCImporter()
        else:
            geo = FlipperGCImporter()
        geo.setTextures(textures)
        try:
            geo.parse(model_data)
        except Exception as e:
            self.report({'ERROR'}, "Parse error: %s" % e)
            return {'CANCELLED'}

        # ── Build scene ──────────────────────────────────────────────────────
        try:
            mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e)
            return {'CANCELLED'}

        if self.extend_clip_distance:
            extend_clip_distance(geo)
        if self.blend_vertex_colors:
            disable_eevee_shadows()
        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = "Standard"
            except Exception:
                pass

        # Import animations (.njm) from the same directory, for .nj / .xj models only
        total_actions = 0
        model_ext = os.path.splitext(filepath)[1].lower()
        if self.import_animations and platform != 'GC' and model_ext in ('.nj', '.xj'):
            model_dir = os.path.dirname(filepath)
            try:
                dir_entries = os.listdir(model_dir)
            except OSError:
                dir_entries = []
            for fname in sorted(dir_entries):
                if os.path.splitext(fname)[1].lower() not in _ANIM_EXTS:
                    continue
                anim_path = os.path.join(model_dir, fname)
                try:
                    with open(anim_path, 'rb') as f:
                        anim_data = f.read()
                    njm = parse_njm(anim_data)
                    if njm is None:
                        self.report({'WARNING'}, "Could not parse animation: %s" % fname)
                        continue
                    action_name = os.path.splitext(fname)[0]
                    build_blender_action(action_name, njm)
                    total_actions += 1
                except Exception as e:
                    self.report({'WARNING'}, "Animation import error for %s: %s" % (fname, e))

        self.report({'INFO'}, "Imported %d mesh(es), %d texture(s), %d action(s) from %s" % (
            mesh_count, len(textures), total_actions, os.path.basename(filepath)))
        return {'FINISHED'}


# ============================================================
# PSO Stage Model operator  (n.rel)
# ============================================================
class IMPORT_OT_pso_stage(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_stage"
    bl_label       = "PSO Stage Model"
    bl_description = "Import a Phantasy Star Online stage map file (n.rel)"
    bl_options     = {'REGISTER', 'UNDO'}

    filename_ext = ".rel"
    filter_glob: StringProperty(default="*n.rel", options={'HIDDEN'})

    platform: EnumProperty(
        name="Platform",
        description="Which version of PSO the file comes from",
        items=_PLATFORM_ITEMS,
        default='AUTO',
    )
    xvm_filepath: StringProperty(
        name="Texture Archive",
        description="Texture archive in the same folder (.xvm / .pvm / .gvm). Leave blank to auto-detect",
        default="",
    )
    blend_vertex_colors: BoolProperty(
        name="Blend Vertex Colors",
        description="Apply vertex colors as lighting in the scene",
        default=True,
    )
    disable_color_correction: BoolProperty(
        name="Disable Color Correction",
        description="Set Color Management to Standard so textures match the original game's appearance",
        default=True,
    )
    extend_clip_distance: BoolProperty(
        name="Extend Viewport Clip Distance",
        description="Raise Clip End so the imported stage is fully visible",
        default=True,
    )
    import_skybox: BoolProperty(
        name="Attempt Sky Import",
        description="Automatically look for a matching skybox file (s.xj / s.nj / s.gj) in the same folder",
        default=True,
    )

    def draw(self, context):
        l = self.layout
        l.prop(self, "platform")
        l.separator()
        if self.platform == 'AUTO':
            ext_hint = '.xvm / .pvm / .gvm'
        else:
            ext_hint = {'BB': '.xvm', 'DC': '.pvm', 'GC': '.gvm'}.get(self.platform, '')
        l.label(text="Texture Archive (%s):" % ext_hint)
        l.prop(self, "xvm_filepath", text="")
        l.label(text="(leave blank to auto-detect)")
        l.separator()
        l.prop(self, "blend_vertex_colors")
        l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")
        l.prop(self, "import_skybox")

    def execute(self, context):
        filepath = self.filepath
        platform = self.platform
        if platform == 'AUTO':
            platform = detect_platform(filepath)
            self.report({'INFO'}, "Autodetected platform: %s" % platform)
        label = "PSO %s Stage" % platform

        # ── Resolve texture archive ──────────────────────────────────────────
        manual_name = self.xvm_filepath.strip()
        # ── Resolve and load texture archive (shared for all platforms) ────────
        textures = []
        tex_path = (os.path.join(os.path.dirname(filepath), manual_name) if manual_name
                    else find_compound_tex_path(filepath) or find_tex_archive(filepath, platform))
        if tex_path and os.path.exists(tex_path):
            try:
                with open(tex_path, 'rb') as f:
                    raw = f.read()
                textures = load_texture_archive(raw)
                msg = "Loaded %d texture(s) from %s" % (len(textures), os.path.basename(tex_path))
                self.report({'INFO'}, msg)
                print("[%s] %s" % (label, msg))
            except Exception as e:
                self.report({'WARNING'}, "Texture load failed: %s" % e)
        else:
            tried = tex_path or "(no texture archive found)"
            self.report({'WARNING'}, "Texture archive not found — tried: %s" % tried)
            print("[%s] Texture archive not found — tried: %s" % (label, tried))

        # ── Parse geometry (platform-specific) ──────────────────────────────
        if platform == 'BB':
            try:
                with open(filepath, 'rb') as f:
                    rel_data = f.read()
            except OSError as e:
                self.report({'ERROR'}, "Cannot open: %s" % e)
                return {'CANCELLED'}

            geo = NinjaStageGeometry()
            geo.setTextures(textures)
            try:
                geo.parse(rel_data)
            except Exception as e:
                self.report({'ERROR'}, "Parse error: %s" % e)
                return {'CANCELLED'}

        elif platform == 'DC':
            # Auto-locate the paired d.rel / n.rel
            stem, ext = os.path.splitext(filepath)
            if stem and stem[-1].lower() in ('n', 'd'):
                base   = stem[:-1]
                # Preserve the original extension case (e.g. .REL not .rel)
                d_path = base + ('D' if stem[-1].isupper() else 'd') + ext
                n_path = base + ('N' if stem[-1].isupper() else 'n') + ext
            else:
                d_path = n_path = filepath

            def _load(p):
                if os.path.exists(p):
                    with open(p, 'rb') as f:
                        return f.read()
                return None

            d_data = _load(d_path)
            n_data = _load(n_path)
            if d_data is None and n_data is None:
                self.report({'ERROR'}, "Cannot find d.rel or n.rel")
                return {'CANCELLED'}

            geo = NinjaDCRelImporter()
            geo.setTextures(textures)
            try:
                geo.parse(d_data, n_data)
            except Exception as e:
                self.report({'ERROR'}, "Parse error: %s" % e)
                return {'CANCELLED'}

        else:  # GC
            try:
                with open(filepath, 'rb') as f:
                    rel_data = f.read()
            except OSError as e:
                self.report({'ERROR'}, "Cannot open: %s" % e)
                return {'CANCELLED'}

            geo = FlipperGCImporter()
            geo.setTextures(textures)
            try:
                geo.parse_stage(rel_data)
            except Exception as e:
                self.report({'ERROR'}, "Parse error: %s" % e)
                return {'CANCELLED'}

        # ── Build scene (shared for all platforms) ───────────────────────────
        try:
            mesh_count = build_blender_scene(geo, filepath, self.blend_vertex_colors)
        except Exception as e:
            self.report({'ERROR'}, "Scene build error: %s" % e)
            return {'CANCELLED'}

        if self.extend_clip_distance:
            extend_clip_distance(geo)
        if self.blend_vertex_colors:
            disable_eevee_shadows()
        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = "Standard"
            except Exception:
                pass

        if self.import_skybox:
            sky_geo = _import_skybox(self, filepath, self.blend_vertex_colors, "[%s]" % label)
            if sky_geo and self.extend_clip_distance:
                extend_clip_distance(sky_geo)

        self.report({'INFO'}, "Imported %d mesh(es), %d texture(s) from %s" % (
            mesh_count, len(textures), os.path.basename(filepath)))
        return {'FINISHED'}

# ============================================================
# NJM animation — coordinate-space helpers
# ============================================================

# Module-level caches populated by build_blender_scene so that
# build_blender_action can convert NJM absolute transforms to
# Blender pose-bone deltas and auto-bind the action slot.
# Both keyed by bone count (int).
_pso_rest_transforms = {}  # {bone_count: {bone_idx: {'pos': tuple3, 'rot': tuple3}}}
_pso_armatures       = []  # [(name_stem, bone_count, arm_obj), ...]


def _longest_common_substring(a, b):
    """Return the length of the longest common substring between a and b."""
    a = a.lower(); b = b.lower()
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > best:
                best = k
    return best


def _mat3_from_njm_euler(rx, ry, rz):
    """3x3 column-vector rotation matrix for NJ intrinsic ZYX (= extrinsic XYZ)."""
    import math as _m
    cx, sx = _m.cos(rx), _m.sin(rx)
    cy, sy = _m.cos(ry), _m.sin(ry)
    cz, sz = _m.cos(rz), _m.sin(rz)
    # Rz * Ry * Rx — matches DashMat4.rotate() application order
    return [
        [ cz*cy,              cz*sy*sx - sz*cx,  cz*sy*cx + sz*sx],
        [ sz*cy,              sz*sy*sx + cz*cx,  sz*sy*cx - cz*sx],
        [-sy,                 cy*sx,              cy*cx           ],
    ]


def _euler_from_mat3_njm(m):
    """Extract NJ ZYX intrinsic euler (rx, ry, rz) from 3x3 column-vector matrix."""
    import math as _m
    sy = -m[2][0]
    sy = max(-1.0, min(1.0, sy))
    ry = _m.asin(sy)
    cy = _m.cos(ry)
    if abs(cy) > 1e-6:
        rx = _m.atan2(m[2][1] / cy, m[2][2] / cy)
        rz = _m.atan2(m[1][0] / cy, m[0][0] / cy)
    else:
        rx = 0.0
        rz = _m.atan2(-m[0][1], m[1][1])
    return (rx, ry, rz)


def _mat3_mul(a, b):
    """3x3 matrix multiply: result = a * b."""
    return [
        [a[0][0]*b[0][0] + a[0][1]*b[1][0] + a[0][2]*b[2][0],
         a[0][0]*b[0][1] + a[0][1]*b[1][1] + a[0][2]*b[2][1],
         a[0][0]*b[0][2] + a[0][1]*b[1][2] + a[0][2]*b[2][2]],
        [a[1][0]*b[0][0] + a[1][1]*b[1][0] + a[1][2]*b[2][0],
         a[1][0]*b[0][1] + a[1][1]*b[1][1] + a[1][2]*b[2][1],
         a[1][0]*b[0][2] + a[1][1]*b[1][2] + a[1][2]*b[2][2]],
        [a[2][0]*b[0][0] + a[2][1]*b[1][0] + a[2][2]*b[2][0],
         a[2][0]*b[0][1] + a[2][1]*b[1][1] + a[2][2]*b[2][1],
         a[2][0]*b[0][2] + a[2][1]*b[1][2] + a[2][2]*b[2][2]],
    ]


def _mat3_vec(m, v):
    """Multiply 3x3 matrix m by 3D column vector v."""
    return (
        m[0][0]*v[0] + m[0][1]*v[1] + m[0][2]*v[2],
        m[1][0]*v[0] + m[1][1]*v[1] + m[1][2]*v[2],
        m[2][0]*v[0] + m[2][1]*v[1] + m[2][2]*v[2],
    )


# ============================================================
# NJM animation parser and Blender Action builder
# ============================================================

def parse_njm(data):
    """Parse an NJM animation file (both NMDM/v2 and BB-footer variants).
    Returns a dict with 'frame_count', 'interp', 'channels', 'tracks', or None on failure."""
    import math
    TWO_PI_OVER_65536 = 2.0 * math.pi / 65536.0

    if len(data) < 4:
        return None

    bs = BitStream(data)
    magic = bs.readUInt()

    if magic == MAGIC_NMDM:
        # v2 / DC / GC: NMDM magic, u32 chunk size, then motion header
        _chunk_size = bs.readUInt()
        motion_start = bs.tell()
    else:
        # BB player format: footer at end of file holds indirection offsets
        if len(data) < 16:
            return None
        try:
            bs.seek(len(data) - 16)
            offset1 = bs.readUInt()
            bs.seek(offset1)
            action_offset = bs.readUInt()
            bs.seek(action_offset + 4)
            motion_start = bs.readUInt()
        except Exception:
            return None

    if motion_start + 12 > len(data):
        return None

    # Motion header: i32 mDataTableOffset, i32 frameCount, u16 type, u16 inpFn
    bs.seek(motion_start)
    m_data_table_rel = bs.readInt()
    frame_count      = bs.readInt()
    motion_type      = bs.readUShort()
    inp_fn           = bs.readUShort()

    interp = inp_fn & 0xFF   # low byte: interpolation type (0=Linear, 1=Spline)
    # NOTE: the node count is NOT stored in inp_fn.  It is implicit in the
    # table size: the table ends where the first keyframe data begins, so we
    # infer element_count below after scanning the table.

    has_position   = bool(motion_type & 0x0001)
    has_euler      = bool(motion_type & 0x0002)
    has_scale      = bool(motion_type & 0x0004)
    has_quaternion = bool(motion_type & 0x2000)

    # Ordered channel list (determines table column order)
    channels = []
    if has_position:   channels.append('position')
    if has_euler:      channels.append('euler')
    if has_quaternion: channels.append('quaternion')
    if has_scale:      channels.append('scale')

    num_channels = len(channels)
    if num_channels == 0:
        return None

    table_abs      = motion_start + m_data_table_rel
    bytes_per_bone = num_channels * 8   # N i32 offsets + N i32 counts (SoA layout)

    # Infer element_count: the table has no stored size; it ends where keyframe
    # data begins.  We detect this by finding the minimum off value in all
    # table entries (where cnt > 0), subject to the constraint that a valid
    # keyframe offset at entry index i must point PAST the current entry:
    #   off >= m_data_table_rel + (i+1) * bytes_per_bone
    # This filters out small frame numbers from keyframe data that would
    # otherwise be misread as table offsets when scanning past the table end.
    min_kf_off = None
    for i in range(512):
        base = table_abs + i * bytes_per_bone
        if base + bytes_per_bone > len(data):
            break
        min_valid = m_data_table_rel + (i + 1) * bytes_per_bone
        for j in range(num_channels):
            off = struct.unpack_from('<i', data, base + j * 4)[0]
            cnt = struct.unpack_from('<i', data, base + num_channels * 4 + j * 4)[0]
            if cnt > 0 and off >= min_valid:
                if min_kf_off is None or off < min_kf_off:
                    min_kf_off = off

    if min_kf_off is None:
        return None

    element_count = (min_kf_off - m_data_table_rel) // bytes_per_bone
    if element_count <= 0:
        return None

    tracks = []
    for bone_idx in range(element_count):
        bone_table_start = table_abs + bone_idx * bytes_per_bone
        if bone_table_start + bytes_per_bone > len(data):
            break

        # Table layout: each channel has a (data_offset, keyframe_count) pair.
        # All offsets come first, then all counts (struct-of-arrays layout).
        bs.seek(bone_table_start)
        ch_offsets = [bs.readInt() for _ in range(num_channels)]
        ch_counts  = [bs.readInt() for _ in range(num_channels)]

        bone_data = {k: [] for k in channels}
        bone_data['bone_index'] = bone_idx

        for ch_idx, ch_name in enumerate(channels):
            off = ch_offsets[ch_idx]
            cnt = ch_counts[ch_idx]
            abs_off = motion_start + off

            if cnt <= 0 or abs_off >= len(data):
                continue

            if ch_name == 'position':
                for k in range(cnt):
                    kf = abs_off + k * 16
                    if kf + 16 > len(data): break
                    bs.seek(kf)
                    frame = bs.readInt()
                    x = bs.readFloat(); y = bs.readFloat(); z = bs.readFloat()
                    bone_data['position'].append((frame, x, y, z))

            elif ch_name == 'euler':
                # Detect compact (8-byte) vs wide (16-byte) encoding.
                # Compact frames are u16 and must be monotonically increasing and < frameCount.
                compact = True
                if cnt > 0:
                    bs.seek(abs_off)
                    first_f = bs.readUShort()
                    if first_f >= frame_count:
                        compact = False
                    else:
                        prev = first_f
                        for k in range(1, min(cnt, 8)):
                            bs.seek(abs_off + k * 8)
                            f = bs.readUShort()
                            if f < prev:
                                compact = False
                                break
                            prev = f

                kf_size = 8 if compact else 16
                for k in range(cnt):
                    kf = abs_off + k * kf_size
                    if kf + kf_size > len(data): break
                    bs.seek(kf)
                    if compact:
                        frame = bs.readUShort()
                        rx = bs.readUShort(); ry = bs.readUShort(); rz = bs.readUShort()
                    else:
                        frame = bs.readInt()
                        rx = bs.readInt(); ry = bs.readInt(); rz = bs.readInt()
                    bone_data['euler'].append((
                        frame,
                        rx * TWO_PI_OVER_65536,
                        ry * TWO_PI_OVER_65536,
                        rz * TWO_PI_OVER_65536,
                    ))

            elif ch_name == 'quaternion':
                for k in range(cnt):
                    kf = abs_off + k * 20
                    if kf + 20 > len(data): break
                    bs.seek(kf)
                    frame = bs.readInt()
                    w = bs.readFloat(); x = bs.readFloat()
                    y = bs.readFloat(); z = bs.readFloat()
                    bone_data['quaternion'].append((frame, w, x, y, z))

            elif ch_name == 'scale':
                for k in range(cnt):
                    kf = abs_off + k * 16
                    if kf + 16 > len(data): break
                    bs.seek(kf)
                    frame = bs.readInt()
                    x = bs.readFloat(); y = bs.readFloat(); z = bs.readFloat()
                    bone_data['scale'].append((frame, x, y, z))

        tracks.append(bone_data)

    return {
        'frame_count':   frame_count,
        'interp':        interp,
        'channels':      channels,
        'tracks':        tracks,
        'element_count': element_count,
    }


def build_blender_action(action_name, njm):
    """Create a bpy.data.actions Action from parsed NJM data. Returns the Action."""
    action              = bpy.data.actions.new(name=action_name)
    action.use_fake_user = True
    interp_type = 'BEZIER' if njm['interp'] == 1 else 'LINEAR'

    # Blender 4.4+ uses a layered action system; earlier versions expose action.fcurves directly.
    # The group keyword also differs: legacy uses 'action_group', channelbag uses 'group_name'.
    if hasattr(action, 'fcurves'):
        fcurves   = action.fcurves
        group_kw  = 'action_group'
    else:
        slot  = action.slots.new(id_type='OBJECT', name="Object")
        layer = action.layers.new(name="Layer")
        strip = layer.strips.new(type='KEYFRAME')
        cb    = strip.channelbag(slot)
        if cb is None:
            if hasattr(strip, 'channelbag_create'):
                cb = strip.channelbag_create(slot)
            else:
                cb = strip.channelbags.new(slot)
        fcurves  = cb.fcurves
        group_kw = 'group_name'

    def new_fc(data_path, index, group):
        return fcurves.new(data_path=data_path, index=index, **{group_kw: group})

    # Find the best-matching armature by name similarity, with bone count as tiebreaker
    element_count = njm.get('element_count', 0)
    arm_obj = None
    if _pso_armatures:
        best_score = -1
        for stem, bone_count, candidate in _pso_armatures:
            lcs = _longest_common_substring(action_name, stem)
            # Prefer longer name match; use bone-count match as a secondary boost
            score = lcs * 2 + (1 if bone_count == element_count else 0)
            if score > best_score:
                best_score = score
                arm_obj = candidate
    rest_xforms = _pso_rest_transforms.get(element_count, {})

    for bone_data in njm['tracks']:
        bone_idx  = bone_data['bone_index']
        bone_name = "bone_%03d" % bone_idx
        prefix    = 'pose.bones["%s"]' % bone_name

        rest      = rest_xforms.get(bone_idx, {})
        rest_pos  = rest.get('pos', (0.0, 0.0, 0.0))
        rest_rot  = rest.get('rot', (0.0, 0.0, 0.0))
        R_rest    = _mat3_from_njm_euler(rest_rot[0], rest_rot[1], rest_rot[2])
        # R_rest^T = R_rest^(-1) for rotation matrices
        R_rest_T  = [[R_rest[j][i] for j in range(3)] for i in range(3)]

        if bone_data.get('position'):
            posed_pos = []
            for (frame, x, y, z) in bone_data['position']:
                delta = (x - rest_pos[0], y - rest_pos[1], z - rest_pos[2])
                pp = _mat3_vec(R_rest_T, delta)
                posed_pos.append((frame, pp[0], pp[1], pp[2]))
            for axis_i in range(3):
                fc = new_fc("%s.location" % prefix, axis_i, bone_name)
                fc.keyframe_points.add(len(posed_pos))
                for ki, (frame, px, py, pz) in enumerate(posed_pos):
                    kp = fc.keyframe_points[ki]
                    kp.co = (float(frame + 1), (px, py, pz)[axis_i])
                    kp.interpolation = interp_type
                fc.update()

        if bone_data.get('euler'):
            posed_euler = []
            for (frame, rx, ry, rz) in bone_data['euler']:
                R_njm  = _mat3_from_njm_euler(rx, ry, rz)
                R_pose = _mat3_mul(R_rest_T, R_njm)
                posed_euler.append((frame,) + _euler_from_mat3_njm(R_pose))
            for axis_i in range(3):
                fc = new_fc("%s.rotation_euler" % prefix, axis_i, bone_name)
                fc.keyframe_points.add(len(posed_euler))
                for ki, (frame, prx, pry, prz) in enumerate(posed_euler):
                    kp = fc.keyframe_points[ki]
                    kp.co = (float(frame + 1), (prx, pry, prz)[axis_i])
                    kp.interpolation = interp_type
                fc.update()

        if bone_data.get('quaternion'):
            for comp_i in range(4):
                fc = new_fc("%s.rotation_quaternion" % prefix, comp_i, bone_name)
                fc.keyframe_points.add(len(bone_data['quaternion']))
                for ki, (frame, w, x, y, z) in enumerate(bone_data['quaternion']):
                    kp = fc.keyframe_points[ki]
                    kp.co = (float(frame + 1), (w, x, y, z)[comp_i])
                    kp.interpolation = interp_type
                fc.update()

        if bone_data.get('scale'):
            for axis_i in range(3):
                fc = new_fc("%s.scale" % prefix, axis_i, bone_name)
                fc.keyframe_points.add(len(bone_data['scale']))
                for ki, (frame, x, y, z) in enumerate(bone_data['scale']):
                    kp = fc.keyframe_points[ki]
                    kp.co = (float(frame + 1), (x, y, z)[axis_i])
                    kp.interpolation = interp_type
                fc.update()

    # Auto-assign the action (and slot, for Blender 5.1+) to the matching armature
    if arm_obj is not None:
        try:
            anim_data = arm_obj.animation_data_create()
            anim_data.action = action
            # Blender 5.1+ layered actions require explicitly binding the slot
            if hasattr(anim_data, 'action_slot') and action.slots:
                anim_data.action_slot = action.slots[0]
        except Exception:
            pass

    return action


class IMPORT_OT_pso_bml(Operator, ImportHelper):
    bl_idname      = "import_scene.pso_bml"
    bl_label       = "Import PSO BML Archive"
    bl_description = (
        "Import a Phantasy Star Online BML model archive. "
        "Extracts all models (.nj / .gj / .xj), their paired textures, "
        "and any animations (.njm) as Blender Actions."
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
    import_animations: BoolProperty(
        name="Import Animations",
        description="Import .njm animations as Blender Actions and build an armature for the model",
        default=True,
    )

    def draw(self, context):
        l = self.layout
        l.prop(self, "blend_vertex_colors")
        l.prop(self, "disable_color_correction")
        l.prop(self, "extend_clip_distance")
        l.prop(self, "import_animations")

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

        pairs      = []
        anim_entries = []   # .njm entries found in the archive
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
            elif ext in _ANIM_EXTS:
                anim_entries.append(entries[i])
            # anything else is silently skipped
            i += 1

        if not pairs and not anim_entries:
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

        total_meshes     = 0
        total_tex        = 0
        combined_geo     = None   # used for clip-distance calculation
        last_bml_textures = []    # textures from the most recent model that had them

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

            # 4. Reuse textures from the previous model in this BML.
            #    LOD/variant models (lo_*, hi_*, sd_*) carry no embedded texture
            #    of their own and are expected to share the set from their sibling,
            #    which always appears directly before them in the archive.
            if not textures and last_bml_textures:
                textures = last_bml_textures
                print("[PSO BML] %s: no texture found — reusing %d texture(s) from previous model" % (
                    name, len(textures)))

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

            if not self.import_animations:
                geo.nodes = []
            try:
                mc = build_blender_scene(geo, name, self.blend_vertex_colors)
            except Exception as e:
                self.report({'WARNING'}, "Scene build error for %s: %s" % (name, e))
                continue

            total_meshes += mc
            total_tex    += len(textures)
            combined_geo  = geo   # keep last for clip-distance (all share world space)
            if textures:
                last_bml_textures = textures

        if combined_geo and self.extend_clip_distance:
            extend_clip_distance(combined_geo)
        if self.blend_vertex_colors:
            disable_eevee_shadows()
        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = "Standard"
            except Exception:
                pass

        # Import animations (.njm) found in the BML archive
        total_actions = 0
        if self.import_animations:
            for anim_entry in anim_entries:
                anim_name = anim_entry['filename']
                try:
                    njm = parse_njm(anim_entry['data'])
                    if njm is None:
                        self.report({'WARNING'}, "Could not parse animation: %s" % anim_name)
                        continue
                    action_name = os.path.splitext(anim_name)[0]
                    build_blender_action(action_name, njm)
                    total_actions += 1
                except Exception as e:
                    self.report({'WARNING'}, "Animation import error for %s: %s" % (anim_name, e))

        msg = "Imported %d mesh(es), %d texture(s), %d action(s) from %s (%d model(s))" % (
            total_meshes, total_tex, total_actions, os.path.basename(filepath), len(pairs))
        self.report({'INFO'}, msg)
        print("[PSO BML] " + msg)
        return {'FINISHED'}


# ============================================================
# GSL archive parser
# ============================================================
def gsl_read_archive(filepath):
    """Parse a GSL archive and return a list of {'filename': str, 'data': bytes}."""
    with open(filepath, 'rb') as fp:
        fp.seek(0, os.SEEK_END)
        file_size = fp.tell()
        fp.seek(0)

        entries = []
        data_start = file_size  # will shrink as we read the table
        while fp.tell() < data_start:
            raw = fp.read(0x28)
            if len(raw) < 0x28:
                break
            name_bytes, offset_sector, length = struct.unpack_from('<32sII', raw)
            fp.read(0x08)  # 8 bytes padding after each table entry

            name = name_bytes.decode('ascii').rstrip(' \t\r\n\0')
            if not name:
                break

            offset = offset_sector * 2048
            if offset < data_start:
                data_start = offset
            entries.append({'filename': name, 'offset': offset, 'length': length})

        result = []
        for e in entries:
            fp.seek(e['offset'])
            result.append({'filename': e['filename'], 'data': fp.read(e['length'])})

    return result


# ============================================================
# Sidebar panel properties
# ============================================================
class PSO_PG_panel_props(PropertyGroup):
    gsl_filepath: StringProperty(
        name="GSL File",
        description="Path to the GSL archive to extract",
        default="",
        subtype='FILE_PATH',
    )
    gsl_output_dir: StringProperty(
        name="Output Folder",
        description="Directory where extracted files will be written",
        default="",
        subtype='DIR_PATH',
    )


# ============================================================
# GSL extract operator
# ============================================================
class PSO_OT_extract_gsl(Operator):
    bl_idname      = "pso.extract_gsl"
    bl_label       = "Extract GSL"
    bl_description = "Extract all files from the selected GSL archive to the output folder"

    def execute(self, context):
        props = context.scene.pso_panel

        gsl_path = bpy.path.abspath(props.gsl_filepath).strip()
        out_dir  = bpy.path.abspath(props.gsl_output_dir).strip()

        if not gsl_path:
            self.report({'ERROR'}, "No GSL file specified")
            return {'CANCELLED'}
        if not os.path.isfile(gsl_path):
            self.report({'ERROR'}, "GSL file not found: %s" % gsl_path)
            return {'CANCELLED'}
        if not out_dir:
            self.report({'ERROR'}, "No output folder specified")
            return {'CANCELLED'}

        os.makedirs(out_dir, exist_ok=True)

        try:
            entries = gsl_read_archive(gsl_path)
        except Exception as e:
            self.report({'ERROR'}, "Failed to read GSL: %s" % e)
            return {'CANCELLED'}

        if not entries:
            self.report({'WARNING'}, "GSL archive is empty or unreadable")
            return {'CANCELLED'}

        for entry in entries:
            dest = os.path.join(out_dir, entry['filename'])
            try:
                with open(dest, 'wb') as f:
                    f.write(entry['data'])
            except OSError as e:
                self.report({'WARNING'}, "Could not write %s: %s" % (entry['filename'], e))

        self.report({'INFO'}, "Extracted %d file(s) to %s" % (len(entries), out_dir))
        return {'FINISHED'}


# ============================================================
# Viewport sidebar panel
# ============================================================
class PSO_PT_sidebar(Panel):
    bl_label       = "PSO Tools"
    bl_idname      = "PSO_PT_sidebar"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "PSO"

    def draw(self, context):
        pass   # sub-panels carry the content


class PSO_PT_gsl_tools(Panel):
    bl_label       = "GSL Extraction"
    bl_idname      = "PSO_PT_gsl_tools"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "PSO"
    bl_parent_id   = "PSO_PT_sidebar"

    def draw(self, context):
        l     = self.layout
        props = context.scene.pso_panel

        l.label(text="GSL Archive:")
        l.prop(props, "gsl_filepath", text="")
        l.label(text="Output Folder:")
        l.prop(props, "gsl_output_dir", text="")
        l.separator()
        l.operator("pso.extract_gsl", icon='EXPORT')


# ============================================================
# Menu hooks
# ============================================================
def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_pso_actor.bl_idname, text="PSO Actor Model (.xj/.nj/.gj)")
    self.layout.operator(IMPORT_OT_pso_stage.bl_idname, text="PSO Stage Model (n.rel)")
    self.layout.operator(IMPORT_OT_pso_bml.bl_idname,   text="PSO BML Archive (.bml)")

# ============================================================
# Registration
# ============================================================
_CLASSES = (
    IMPORT_OT_pso_actor,
    IMPORT_OT_pso_stage,
    IMPORT_OT_pso_bml,
    PSO_PG_panel_props,
    PSO_OT_extract_gsl,
    PSO_PT_sidebar,
    PSO_PT_gsl_tools,
)

def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pso_panel = PointerProperty(type=PSO_PG_panel_props)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.pso_panel
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
