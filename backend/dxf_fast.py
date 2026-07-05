"""
Python ctypes binding for the C-based fast DXF parser.
"""
import ctypes
import os
import re
import sys
from pathlib import Path
import numpy as np
from color_utils import aci_to_hex

import platform

# Locate the dynamic library (DLL on Windows, SO on Linux)
_HERE = Path(__file__).parent
if platform.system() == "Windows":
    _LIB_NAME = "dxf_fast_parser.dll"
else:
    _LIB_NAME = "dxf_fast_parser.so"

_DLL_PATH = _HERE / _LIB_NAME

if not _DLL_PATH.exists():
    raise FileNotFoundError(
        f"{_LIB_NAME} not found at {_DLL_PATH}. "
        f"Ensure it is compiled for this platform."
    )

_lib = ctypes.CDLL(str(_DLL_PATH))


class ParseResult(ctypes.Structure):
    _fields_ = [
        ("verts", ctypes.POINTER(ctypes.c_float)),
        ("vert_counts", ctypes.POINTER(ctypes.c_int)),
        ("batch_layers", ctypes.POINTER(ctypes.c_char)),
        ("batch_colors", ctypes.POINTER(ctypes.c_int)),
        ("n_batches", ctypes.c_int),
        ("total_vert_count", ctypes.c_int),
        ("total_seg_count", ctypes.c_int),
        ("layer_names", ctypes.POINTER(ctypes.c_char)),
        ("layer_colors", ctypes.POINTER(ctypes.c_int)),
        ("n_layers", ctypes.c_int),
        ("bbox", ctypes.c_float * 4),
        ("verts_base", ctypes.POINTER(ctypes.c_float)),
        ("layer_names_base", ctypes.POINTER(ctypes.c_char)),
    ]


_lib.parse_dxf.argtypes = [ctypes.c_char_p, ctypes.POINTER(ParseResult)]
_lib.parse_dxf.restype = ctypes.c_int

_lib.free_dxf_result.argtypes = [ctypes.POINTER(ParseResult)]
_lib.free_dxf_result.restype = None


_DXF_CODEPAGE_MAP = {
    "ANSI_1252": "cp1252",
    "ANSI_1251": "cp1251",
    "ANSI_1250": "cp1250",
    "ANSI_1253": "cp1253",
    "ANSI_1254": "cp1254",
    "ANSI_1255": "cp1255",
    "ANSI_1256": "cp1256",
    "ANSI_1257": "cp1257",
    "ANSI_1258": "cp1258",
    "ANSI_874": "cp874",
    "ANSI_932": "cp932",
    "ANSI_936": "gbk",
    "ANSI_949": "cp949",
    "ANSI_950": "cp950",
    "UTF-8": "utf-8",
    "UTF8": "utf-8",
    "ISO-8859-1": "iso-8859-1",
}


def _detect_dxf_encoding(filepath: str) -> str:
    """Read the DXF header and return the text encoding declared by $DWGCODEPAGE."""
    try:
        with open(filepath, "rb") as f:
            # Only inspect the first 8 KB; $DWGCODEPAGE is always in the header
            data = f.read(8192)
        # Look for: 9\n$DWGCODEPAGE\n  3\n<CODEPAGE>\n
        m = re.search(
            rb"\$DWGCODEPAGE\s+3\s+([A-Za-z0-9_\-]+)",
            data,
            re.IGNORECASE,
        )
        if m:
            codepage = m.group(1).decode("ascii", errors="ignore").upper()
            mapped = _DXF_CODEPAGE_MAP.get(codepage)
            if mapped:
                return mapped
    except Exception:
        pass
    return "cp1252"  # sensible default for many AutoCAD DXF files


def _safe_decode(raw: bytes, encoding: str) -> str:
    """Decode raw bytes using the detected encoding, falling back gracefully."""
    for enc in (encoding, "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def parse_dxf_fast(filepath: str) -> dict:
    """
    Parse a DXF file using the optimized C parser.
    Returns a dict with: batches, layers, bbox, origin, stats.
    """
    encoding = _detect_dxf_encoding(filepath)

    result = ParseResult()
    rc = _lib.parse_dxf(filepath.encode("utf-8"), ctypes.byref(result))
    if rc != 0:
        raise IOError(f"Failed to parse DXF: {filepath}")

    try:
        # Get vertex data as numpy array (zero-copy)
        if result.total_vert_count > 0:
            all_verts = np.ctypeslib.as_array(result.verts_base, shape=(result.total_vert_count,))
        else:
            all_verts = np.empty(0, dtype=np.float32)

        # Get per-batch counts
        vert_counts = [result.vert_counts[i] for i in range(result.n_batches)]

        # Get batch layer names
        raw_batch_layers = ctypes.string_at(result.batch_layers, result.n_batches * 256)
        batch_layer_names = []
        for i in range(result.n_batches):
            name = _safe_decode(
                raw_batch_layers[i * 256:(i + 1) * 256].split(b"\x00", 1)[0],
                encoding,
            ).strip()
            batch_layer_names.append(name)

        batch_colors = [result.batch_colors[i] for i in range(result.n_batches)]

        # Build render_batches
        offset = 0
        render_batches = []
        for i in range(result.n_batches):
            count = vert_counts[i]
            slice_verts = all_verts[offset:offset + count]
            render_batches.append({
                "layer": batch_layer_names[i] or "0",
                "color": aci_to_hex(batch_colors[i]),
                "verts": slice_verts.copy(),  # copy to detach from C memory
                "entity_ids": [],
                "vertex_count": count // 2,
            })
            offset += count

        # Layer info
        raw_layer_names = ctypes.string_at(result.layer_names_base, result.n_layers * 256)
        layers = {}
        for i in range(result.n_layers):
            name = _safe_decode(
                raw_layer_names[i * 256:(i + 1) * 256].split(b"\x00", 1)[0],
                encoding,
            ).strip()
            if not name:
                continue
            aci = result.layer_colors[i]
            layers[name] = {
                "name": name,
                "color": aci_to_hex(aci) if aci >= 0 else "#CCCCCC",
                "visible": True,
                "locked": False,
                "frozen": False,
            }

        # Apply origin offset (subtract min coord to preserve Float32 precision)
        bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y = result.bbox
        if not (np.isfinite(bbox_min_x) and np.isfinite(bbox_min_y)):
            bbox_min_x = bbox_min_y = 0.0
            bbox_max_x = bbox_max_y = 0.0

        origin_x, origin_y = bbox_min_x, bbox_min_y
        for b in render_batches:
            v = b["verts"]
            if len(v) > 0:
                v[0::2] -= origin_x
                v[1::2] -= origin_y

        # Best-effort layer colors: the C parser only registers layer names from
        # entity code 8, so it doesn't know the layer color from the LAYER table.
        # Use the first batch color seen for each layer so the UI isn't all gray.
        for b in render_batches:
            layer = layers.get(b["layer"])
            if layer and layer["color"] == "#CCCCCC":
                layer["color"] = b["color"]

        return {
            "layers": layers,
            "batches": render_batches,
            "bbox": {
                "minX": float(bbox_min_x),
                "minY": float(bbox_min_y),
                "maxX": float(bbox_max_x),
                "maxY": float(bbox_max_y),
            },
            "origin": {"x": float(origin_x), "y": float(origin_y)},
            "stats": {
                "total_batches": result.n_batches,
                "total_segments": result.total_seg_count,
                "total_entities": 0,  # Not counted by C parser
            }
        }
    finally:
        _lib.free_dxf_result(ctypes.byref(result))



if __name__ == "__main__":
    import time
    current_dir = os.path.dirname(os.path.abspath(__file__))
    default_path = os.path.join(current_dir, "..", "current.dxf")
    filepath = sys.argv[1] if len(sys.argv) > 1 else default_path

    print(f"Parsing: {filepath}")
    print(f"Size: {os.path.getsize(filepath) / 1024 / 1024:.1f} MB")

    t0 = time.time()
    result = parse_dxf_fast(filepath)
    t1 = time.time()

    print(f"Time: {t1-t0:.2f}s")
    print(f"Batches: {result['stats']['total_batches']}")
    print(f"Segments: {result['stats']['total_segments']}")
    print(f"Layers: {len(result['layers'])}")
    print(f"BBox: {result['bbox']}")
    print(f"Origin: {result['origin']}")

    total_verts = sum(b['verts'].size for b in result['batches'])
    print(f"Total vertices: {total_verts}")