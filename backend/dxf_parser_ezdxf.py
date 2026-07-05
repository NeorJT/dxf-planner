"""
Módulo para cargar, parsear y serializar archivos DXF.
Extrae geometría optimizada para renderizado WebGL (batching por tipo y capa).
"""

import math
import struct
import json
import numpy as np
import ezdxf
from fastapi import HTTPException

from color_utils import ACI_COLORS, aci_to_hex

# Use fast C-based parser if available
try:
    from dxf_fast import parse_dxf_fast
    _USE_FAST_PARSER = True
except (ImportError, FileNotFoundError):
    _USE_FAST_PARSER = False

def _adaptive_segments(radius, total_angle_rad):
    if radius <= 0:
        return 8
    arc_len = abs(total_angle_rad) * radius
    return max(8, min(64, int(arc_len / 4)))

def _add_to_batch(batch_arrays, batch_entity_ids, key, layer_name, color, entity_id, xs_pairs, ys_pairs):
    if key not in batch_arrays:
        batch_arrays[key] = {"layer": layer_name, "color": color, "xs": [], "ys": []}
        batch_entity_ids[key] = []
    batch_arrays[key]["xs"].append(xs_pairs)
    batch_arrays[key]["ys"].append(ys_pairs)
    batch_entity_ids[key].append(entity_id)

def _update_bbox(bbox, *coords):
    for i in range(0, len(coords), 2):
        x, y = coords[i], coords[i + 1]
        if x < bbox[0]: bbox[0] = x
        if y < bbox[1]: bbox[1] = y
        if x > bbox[2]: bbox[2] = x
        if y > bbox[3]: bbox[3] = y

def _process_line_segments(entity, layer_name, color, key, entity_id,
                           batch_arrays, batch_entity_ids, bbox):
    dxftype = entity.dxftype()
    xs_pairs = None
    ys_pairs = None

    if dxftype == "LINE":
        s = entity.dxf.start
        e = entity.dxf.end
        xs_pairs = np.array([s.x, e.x], dtype=np.float32)
        ys_pairs = np.array([s.y, e.y], dtype=np.float32)
        _update_bbox(bbox, s.x, s.y, e.x, e.y)

    elif dxftype == "ARC":
        cx, cy = entity.dxf.center.x, entity.dxf.center.y
        r = entity.dxf.radius
        start = math.radians(entity.dxf.start_angle)
        end = math.radians(entity.dxf.end_angle)
        if end <= start:
            end += 2 * math.pi
        segments = _adaptive_segments(r, end - start)
        angles = np.linspace(start, end, segments)
        xs = cx + r * np.cos(angles)
        ys = cy + r * np.sin(angles)
        n = len(xs) - 1
        xs_pairs = np.empty(n * 2, dtype=np.float32)
        ys_pairs = np.empty(n * 2, dtype=np.float32)
        xs_pairs[0::2] = xs[:-1]; xs_pairs[1::2] = xs[1:]
        ys_pairs[0::2] = ys[:-1]; ys_pairs[1::2] = ys[1:]
        _update_bbox(bbox, cx - r, cy - r, cx + r, cy + r)

    elif dxftype == "CIRCLE":
        cx, cy = entity.dxf.center.x, entity.dxf.center.y
        r = entity.dxf.radius
        segments = _adaptive_segments(r, 2 * math.pi)
        angles = np.linspace(0, 2 * math.pi, segments + 1)
        xs = cx + r * np.cos(angles)
        ys = cy + r * np.sin(angles)
        n = len(xs) - 1
        xs_pairs = np.empty(n * 2, dtype=np.float32)
        ys_pairs = np.empty(n * 2, dtype=np.float32)
        xs_pairs[0::2] = xs[:-1]; xs_pairs[1::2] = xs[1:]
        ys_pairs[0::2] = ys[:-1]; ys_pairs[1::2] = ys[1:]
        _update_bbox(bbox, cx - r, cy - r, cx + r, cy + r)

    elif dxftype in ("LWPOLYLINE", "POLYLINE"):
        if dxftype == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in entity.get_points()]
        else:
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
        if len(pts) < 2:
            return None
        is_closed = entity.is_closed if hasattr(entity, "is_closed") else False
        n = len(pts)
        n_segs = n if is_closed else n - 1
        xs_pairs = np.empty(n_segs * 2, dtype=np.float32)
        ys_pairs = np.empty(n_segs * 2, dtype=np.float32)
        for i in range(n_segs):
            j = (i + 1) % n
            xs_pairs[i * 2] = pts[i][0]
            xs_pairs[i * 2 + 1] = pts[j][0]
            ys_pairs[i * 2] = pts[i][1]
            ys_pairs[i * 2 + 1] = pts[j][1]
        flat_x = np.array([p[0] for p in pts], dtype=np.float32)
        flat_y = np.array([p[1] for p in pts], dtype=np.float32)
        _update_bbox(bbox, flat_x.min(), flat_y.min(), flat_x.max(), flat_y.max())

    elif dxftype == "SPLINE":
        pts = [(p[0], p[1]) for p in entity.flattening(0.5)]
        if len(pts) < 2:
            return None
        arr_x = np.array([p[0] for p in pts], dtype=np.float32)
        arr_y = np.array([p[1] for p in pts], dtype=np.float32)
        n_segs = len(arr_x) - 1
        xs_pairs = np.empty(n_segs * 2, dtype=np.float32)
        ys_pairs = np.empty(n_segs * 2, dtype=np.float32)
        xs_pairs[0::2] = arr_x[:-1]; xs_pairs[1::2] = arr_x[1:]
        ys_pairs[0::2] = arr_y[:-1]; ys_pairs[1::2] = arr_y[1:]
        _update_bbox(bbox, arr_x.min(), arr_y.min(), arr_x.max(), arr_y.max())

    elif dxftype == "ELLIPSE":
        cx = entity.dxf.center.x
        cy = entity.dxf.center.y
        major = entity.dxf.major_axis
        ratio = entity.dxf.ratio
        start = entity.dxf.start_param
        end = entity.dxf.end_param
        if abs(end - start) < 1e-10:
            end = start + 2 * math.pi
        a = math.hypot(major.x, major.y)
        b = a * ratio
        if a <= 0 or b <= 0:
            return None
        segs = _adaptive_segments(max(a, b), abs(end - start))
        angles = np.linspace(start, end, segs + 1)
        rot = math.atan2(major.y, major.x)
        xs = cx + a * np.cos(angles) * math.cos(rot) - b * np.sin(angles) * math.sin(rot)
        ys = cy + a * np.cos(angles) * math.sin(rot) + b * np.sin(angles) * math.cos(rot)
        n = len(xs) - 1
        xs_pairs = np.empty(n * 2, dtype=np.float32)
        ys_pairs = np.empty(n * 2, dtype=np.float32)
        xs_pairs[0::2] = xs[:-1]; xs_pairs[1::2] = xs[1:]
        ys_pairs[0::2] = ys[:-1]; ys_pairs[1::2] = ys[1:]
        _update_bbox(bbox, xs.min(), ys.min(), xs.max(), ys.max())

    elif dxftype == "SOLID":
        try:
            pts = []
            for attr in ("first", "second", "third", "fourth"):
                p = entity.dxf.get(attr)
                if p is not None:
                    pts.append((p.x, p.y))
            if len(pts) >= 2:
                n = len(pts)
                xs_pairs = np.empty(n * 2, dtype=np.float32)
                ys_pairs = np.empty(n * 2, dtype=np.float32)
                for i in range(n):
                    j = (i + 1) % n
                    xs_pairs[i * 2] = pts[i][0]
                    xs_pairs[i * 2 + 1] = pts[j][0]
                    ys_pairs[i * 2] = pts[i][1]
                    ys_pairs[i * 2 + 1] = pts[j][1]
                flat_x = np.array([p[0] for p in pts], dtype=np.float32)
                flat_y = np.array([p[1] for p in pts], dtype=np.float32)
                _update_bbox(bbox, flat_x.min(), flat_y.min(), flat_x.max(), flat_y.max())
        except Exception:
            return None

    else:
        return None

    if xs_pairs is not None:
        _add_to_batch(batch_arrays, batch_entity_ids, key, layer_name, color, entity_id, xs_pairs, ys_pairs)
        return True
    return None

def parse_dxf_to_render_data(filepath: str) -> dict:
    if _USE_FAST_PARSER:
        try:
            return parse_dxf_fast(filepath)
        except Exception as e:
            print(f"Fast parser failed ({e}), falling back to ezdxf")

    try:
        doc = ezdxf.readfile(filepath)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error leyendo DXF: {str(e)}")

    msp = doc.modelspace()
    layers_info = {}

    for layer in doc.layers:
        layers_info[layer.dxf.name] = {
            "name": layer.dxf.name,
            "color": aci_to_hex(layer.color),
            "visible": not layer.is_off(),
            "locked": layer.is_locked(),
            "frozen": layer.is_frozen(),
        }

    batch_arrays = {}
    batch_entity_ids = {}
    bbox = [float("inf"), float("inf"), float("-inf"), float("-inf")]

    entity_index = 0
    block_cache = {}

    def _get_block_geometry(block_name, doc):
        if block_name in block_cache:
            return block_cache[block_name]
        block = doc.blocks.get(block_name)
        if block is None:
            block_cache[block_name] = None
            return None
        
        base_x = block.block.dxf.base_point.x
        base_y = block.block.dxf.base_point.y
        
        block_geom = []
        for ent in block:
            try:
                dxftype = ent.dxftype()
                if dxftype == "LINE":
                    s, e = ent.dxf.start, ent.dxf.end
                    block_geom.append(("LINE", [s.x - base_x, s.y - base_y, e.x - base_x, e.y - base_y]))
                elif dxftype == "CIRCLE":
                    block_geom.append(("CIRCLE", [ent.dxf.center.x - base_x, ent.dxf.center.y - base_y, ent.dxf.radius]))
                elif dxftype == "ARC":
                    block_geom.append(("ARC", [ent.dxf.center.x - base_x, ent.dxf.center.y - base_y,
                                               ent.dxf.radius, ent.dxf.start_angle, ent.dxf.end_angle]))
            except Exception:
                pass
        block_cache[block_name] = block_geom if block_geom else None
        return block_cache[block_name]

    def _resolve_color(entity, layer_obj):
        color = layer_obj.get("color", "#CCCCCC")
        entity_color = entity.dxf.get("color", 256)
        if entity_color != 256 and entity_color in ACI_COLORS:
            color = aci_to_hex(entity_color)
        elif entity_color != 256 and entity_color != 0:
            color = aci_to_hex(entity_color)
        return color

    for entity in msp:
        try:
            layer_name = entity.dxf.get("layer", "0")
            layer_obj = layers_info.get(layer_name, {})
            color = _resolve_color(entity, layer_obj)
            key = f"{layer_name}::{color}"
            entity_id = f"e_{entity_index}"
            entity_index += 1
            dxftype = entity.dxftype()

            if dxftype == "INSERT":
                try:
                    block_name = entity.dxf.name
                    insert_x = entity.dxf.insert.x
                    insert_y = entity.dxf.insert.y
                    scale_x = entity.dxf.get("xscale", 1.0)
                    scale_y = entity.dxf.get("yscale", 1.0)
                    rotation = math.radians(entity.dxf.get("rotation", 0.0))
                    has_rotation = abs(rotation) > 1e-10
                    cos_r = math.cos(rotation) if has_rotation else 1.0
                    sin_r = math.sin(rotation) if has_rotation else 0.0

                    block_geom = _get_block_geometry(block_name, doc)
                    if block_geom:
                        for geom_type, geom_data in block_geom:
                            if geom_type == "LINE":
                                x0, y0, x1, y1 = geom_data
                                dx0, dy0 = x0 * scale_x, y0 * scale_y
                                dx1, dy1 = x1 * scale_x, y1 * scale_y
                                if has_rotation:
                                    rx0 = dx0 * cos_r - dy0 * sin_r + insert_x
                                    ry0 = dx0 * sin_r + dy0 * cos_r + insert_y
                                    rx1 = dx1 * cos_r - dy1 * sin_r + insert_x
                                    ry1 = dx1 * sin_r + dy1 * cos_r + insert_y
                                else:
                                    rx0 = dx0 + insert_x
                                    ry0 = dy0 + insert_y
                                    rx1 = dx1 + insert_x
                                    ry1 = dy1 + insert_y
                                xs_p = np.array([rx0, rx1], dtype=np.float32)
                                ys_p = np.array([ry0, ry1], dtype=np.float32)
                                _update_bbox(bbox, rx0, ry0, rx1, ry1)
                                _add_to_batch(batch_arrays, batch_entity_ids, key, layer_name, color, entity_id, xs_p, ys_p)

                            elif geom_type == "CIRCLE":
                                cx_b, cy_b, r_b = geom_data
                                if r_b > 0:
                                    if has_rotation:
                                        tx_b = cx_b * scale_x
                                        ty_b = cy_b * scale_y
                                        rcx = tx_b * cos_r - ty_b * sin_r + insert_x
                                        rcy = tx_b * sin_r + ty_b * cos_r + insert_y
                                    else:
                                        rcx = cx_b * scale_x + insert_x
                                        rcy = cy_b * scale_y + insert_y

                                    segs = _adaptive_segments(r_b * max(abs(scale_x), abs(scale_y)), 2 * math.pi)
                                    angles = np.linspace(0, 2 * math.pi, segs + 1)
                                    lx = r_b * np.cos(angles)
                                    ly = r_b * np.sin(angles)
                                    lx_s = lx * scale_x
                                    ly_s = ly * scale_y

                                    if has_rotation:
                                        xs = lx_s * cos_r - ly_s * sin_r + rcx
                                        ys = lx_s * sin_r + ly_s * cos_r + rcy
                                    else:
                                        xs = lx_s + rcx
                                        ys = ly_s + rcy

                                    n = len(xs) - 1
                                    xs_p = np.empty(n * 2, dtype=np.float32)
                                    ys_p = np.empty(n * 2, dtype=np.float32)
                                    xs_p[0::2] = xs[:-1]; xs_p[1::2] = xs[1:]
                                    ys_p[0::2] = ys[:-1]; ys_p[1::2] = ys[1:]
                                    _update_bbox(bbox, xs.min(), ys.min(), xs.max(), ys.max())
                                    _add_to_batch(batch_arrays, batch_entity_ids, key, layer_name, color, entity_id, xs_p, ys_p)

                            elif geom_type == "ARC":
                                cx_b, cy_b, r_b, start_deg, end_deg = geom_data
                                if r_b > 0:
                                    if has_rotation:
                                        tx_b = cx_b * scale_x
                                        ty_b = cy_b * scale_y
                                        rcx = tx_b * cos_r - ty_b * sin_r + insert_x
                                        rcy = tx_b * sin_r + ty_b * cos_r + insert_y
                                    else:
                                        rcx = cx_b * scale_x + insert_x
                                        rcy = cy_b * scale_y + insert_y

                                    if end_deg <= start_deg:
                                        end_deg += 360.0
                                    sa_rad = math.radians(start_deg)
                                    ea_rad = math.radians(end_deg)

                                    segs = _adaptive_segments(r_b * max(abs(scale_x), abs(scale_y)), ea_rad - sa_rad)
                                    if segs < 2:
                                        segs = 2
                                    angles = np.linspace(sa_rad, ea_rad, segs)
                                    lx = r_b * np.cos(angles)
                                    ly = r_b * np.sin(angles)
                                    lx_s = lx * scale_x
                                    ly_s = ly * scale_y

                                    if has_rotation:
                                        xs = lx_s * cos_r - ly_s * sin_r + rcx
                                        ys = lx_s * sin_r + ly_s * cos_r + rcy
                                    else:
                                        xs = lx_s + rcx
                                        ys = ly_s + rcy

                                    n = len(xs) - 1
                                    xs_p = np.empty(n * 2, dtype=np.float32)
                                    ys_p = np.empty(n * 2, dtype=np.float32)
                                    xs_p[0::2] = xs[:-1]; xs_p[1::2] = xs[1:]
                                    ys_p[0::2] = ys[:-1]; ys_p[1::2] = ys[1:]
                                    _update_bbox(bbox, xs.min(), ys.min(), xs.max(), ys.max())
                                    _add_to_batch(batch_arrays, batch_entity_ids, key, layer_name, color, entity_id, xs_p, ys_p)
                except Exception:
                    pass

            else:
                _process_line_segments(entity, layer_name, color, key, entity_id,
                                       batch_arrays, batch_entity_ids, bbox)
        except Exception:
            pass

    # Scene origin offset (center of bounding box) to prevent Float32 precision issues
    origin_x = (bbox[0] + bbox[2]) / 2 if bbox[0] != float("inf") else 0.0
    origin_y = (bbox[1] + bbox[3]) / 2 if bbox[1] != float("inf") else 0.0

    batches = []
    for key, data in batch_arrays.items():
        xs_list = data["xs"]
        ys_list = data["ys"]
        if not xs_list:
            continue
        xs = np.concatenate(xs_list) - origin_x
        ys = np.concatenate(ys_list) - origin_y

        n = len(xs)
        verts = np.empty(n * 2, dtype=np.float32)
        verts[0::2] = xs
        verts[1::2] = ys

        batches.append({
            "layer": data["layer"],
            "color": data["color"],
            "verts": verts,
            "entity_ids": batch_entity_ids[key],
        })

    # Fallback bounds if empty
    if bbox[0] == float("inf"):
        bbox = [0.0, 0.0, 0.0, 0.0]

    return {
        "batches": batches,
        "layers": layers_info,
        "bbox": {
            "minX": float(bbox[0]), "minY": float(bbox[1]),
            "maxX": float(bbox[2]), "maxY": float(bbox[3])
        },
        "origin": {"x": float(origin_x), "y": float(origin_y)},
        "stats": {
            "total_batches": len(batches),
            "total_segments": sum(len(b["verts"]) // 4 for b in batches),
            "total_entities": entity_index,
        }
    }

def serialize_render_data(render_data: dict) -> bytes:
    batch_info = []
    verts_parts = []
    for b in render_data["batches"]:
        verts = b["verts"]
        if verts is None:
            continue
        batch_info.append({
            "layer": b["layer"],
            "color": b["color"],
            "vertex_count": len(verts) // 2,
            "entity_ids": b["entity_ids"],
        })
        verts_parts.append(verts)

    all_verts = np.concatenate(verts_parts) if verts_parts else np.empty(0, dtype=np.float32)
    verts_bytes = all_verts.tobytes()

    batch_counts = [bi["vertex_count"] for bi in batch_info]
    header = struct.pack("<I", len(batch_counts))
    if batch_counts:
        header += struct.pack(f"<{len(batch_counts)}I", *batch_counts)

    meta = {
        "plan_id": render_data.get("plan_id", ""),
        "filename": render_data.get("filename", ""),
        "layers": render_data["layers"],
        "batch_info": batch_info,
        "bbox": render_data["bbox"],
        "origin": render_data["origin"],
        "stats": render_data["stats"],
    }

    meta_json = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    meta_len = len(meta_json)
    # Pad meta_json so the next field starts at a 4-byte boundary
    padding_len = (4 - (meta_len % 4)) % 4
    padding = b"\x00" * padding_len

    return (
        meta_len.to_bytes(4, "little")
        + meta_json
        + padding
        + header
        + verts_bytes
    )

def build_pf_segments_fast(render_data: dict) -> np.ndarray:
    batches = render_data["batches"]
    total_segs = sum(len(b["verts"]) // 4 for b in batches)
    if total_segs == 0:
        return np.empty((0, 4), dtype=np.float32)
    segments = np.empty((total_segs, 4), dtype=np.float32)
    offset = 0
    origin_x = render_data["origin"]["x"]
    origin_y = render_data["origin"]["y"]
    for b in batches:
        v = b["verts"]
        if v is None:
            continue
        n_segs = len(v) // 4
        flat = v.reshape(n_segs, 4)
        segments[offset:offset + n_segs, 0] = flat[:, 0] + origin_x
        segments[offset:offset + n_segs, 1] = flat[:, 1] + origin_y
        segments[offset:offset + n_segs, 2] = flat[:, 2] + origin_x
        segments[offset:offset + n_segs, 3] = flat[:, 3] + origin_y
        offset += n_segs
    return segments[:offset]
