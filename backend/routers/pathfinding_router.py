"""
Router para generación de rutas peatonales sobre planos DXF.
Optimizado con numpy vectorizado para grids grandes (1M+ segmentos).
"""

import heapq
import time
import logging
import math
import numpy as np
from collections import deque
from itertools import permutations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from scipy.ndimage import binary_dilation, distance_transform_edt, label

log = logging.getLogger("pathfinding")
log.setLevel(logging.DEBUG)

_fh = logging.FileHandler("pathfinding.log", mode="w", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
log.addHandler(_fh)

_sh = logging.StreamHandler()
_sh.setLevel(logging.DEBUG)
_sh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
log.addHandler(_sh)

router = APIRouter()

_grids_cache: dict = {}


class PassThroughZone(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class AvoidZone(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class PathRequest(BaseModel):
    plan_id: str
    origin: dict
    destination: dict
    clearance: float = Field(default=0.5, ge=0, le=50)
    grid_resolution: Optional[float] = None
    max_distance: Optional[float] = None
    avoid_layers: Optional[list] = None
    pass_through_zones: Optional[list[PassThroughZone]] = None
    avoid_zones: Optional[list[AvoidZone]] = None


class WaypointsRequest(BaseModel):
    plan_id: str
    waypoints: list
    clearance: float = Field(default=0.5, ge=0, le=50)
    grid_resolution: Optional[float] = None
    pass_through_zones: Optional[list[PassThroughZone]] = None
    avoid_zones: Optional[list[AvoidZone]] = None
    optimize_order: Optional[bool] = False


class AnalyzeSpaceRequest(BaseModel):
    segments: list


def _get_plan_segments(plan_id: str):
    from routers.dxf_router import _loaded_plans
    if plan_id not in _loaded_plans:
        raise HTTPException(status_code=404, detail="Plan no encontrado")
    return _loaded_plans[plan_id]


def _build_grid_fast(segments, resolution, bbox, wall_thickness=150.0):
    """Grid binario — engrosa cada segmento con un rectángulo de wall_thickness. Vectorizado."""
    t_start = time.time()
    min_x, min_y = bbox["minX"], bbox["minY"]
    max_x, max_y = bbox["maxX"], bbox["maxY"]

    width = max_x - min_x
    height = max_y - min_y
    if width <= 0 or height <= 0:
        return None, None, None

    cols = max(1, int(np.ceil(width / resolution)))
    rows = max(1, int(np.ceil(height / resolution)))
    inv_res = 1.0 / resolution
    half_thick = wall_thickness * 0.5 * inv_res

    grid = np.zeros((rows, cols), dtype=np.uint8)

    x0 = (segments[:, 0] - min_x) * inv_res
    y0 = (segments[:, 1] - min_y) * inv_res
    x1 = (segments[:, 2] - min_x) * inv_res
    y1 = (segments[:, 3] - min_y) * inv_res

    dx = x1 - x0
    dy = y1 - y0
    seg_len = np.sqrt(dx * dx + dy * dy)
    seg_len = np.maximum(seg_len, 1e-10)
    nx = -dy / seg_len
    ny = dx / seg_len

    cx0 = x0 + nx * half_thick
    cy0 = y0 + ny * half_thick
    cx1 = x0 - nx * half_thick
    cy1 = y0 - ny * half_thick
    cx2 = x1 + nx * half_thick
    cy2 = y1 + ny * half_thick
    cx3 = x1 - nx * half_thick
    cy3 = y1 - ny * half_thick

    min_c = np.floor(np.minimum(np.minimum(cx0, cx1), np.minimum(cx2, cx3))).astype(np.int32)
    max_c = np.ceil(np.maximum(np.maximum(cx0, cx1), np.maximum(cx2, cx3))).astype(np.int32)
    min_r = np.floor(np.minimum(np.minimum(cy0, cy1), np.minimum(cy2, cy3))).astype(np.int32)
    max_r = np.ceil(np.maximum(np.maximum(cy0, cy1), np.maximum(cy2, cy3))).astype(np.int32)

    min_c = np.clip(min_c, 0, cols - 1)
    max_c = np.clip(max_c, 0, cols - 1)
    min_r = np.clip(min_r, 0, rows - 1)
    max_r = np.clip(max_r, 0, rows - 1)

    span_cols = max_c - min_c + 1
    span_rows = max_r - min_r + 1
    max_span = int(np.max(np.maximum(span_cols, span_rows)))

    if max_span <= 8:
        for i in range(len(segments)):
            for r in range(min_r[i], max_r[i] + 1):
                for c in range(min_c[i], max_c[i] + 1):
                    grid[r, c] = 1
    else:
        for i in range(len(segments)):
            r_lo, r_hi = min_r[i], max_r[i]
            c_lo, c_hi = min_c[i], max_c[i]
            grid[r_lo:r_hi + 1, c_lo:c_hi + 1] = 1

    log.info("Grid rasterize: %.3f s (%d segments, wall_thickness=%.0fmm)", time.time() - t_start, len(segments), wall_thickness)
    return grid.astype(bool), min_x, min_y


def _dilate_grid(grid, clearance_cells):
    if clearance_cells <= 0:
        return grid
    struct = np.ones((clearance_cells * 2 + 1, clearance_cells * 2 + 1), dtype=bool)
    return binary_dilation(grid, structure=struct)


def _apply_pass_through_zones(grid, zones, min_x, min_y, resolution, expand_cells=0):
    """Fuerza como libre (False) todas las celdas dentro de cada zona rectangular, opcionalmente expandidas."""
    if not zones:
        return
    inv_res = 1.0 / resolution
    rows, cols = grid.shape
    for zone in zones:
        gx1 = int((min(zone.x1, zone.x2) - min_x) * inv_res) - expand_cells
        gy1 = int((min(zone.y1, zone.y2) - min_y) * inv_res) - expand_cells
        gx2 = int((max(zone.x1, zone.x2) - min_x) * inv_res) + expand_cells
        gy2 = int((max(zone.y1, zone.y2) - min_y) * inv_res) + expand_cells
        gx1 = max(0, gx1)
        gy1 = max(0, gy1)
        gx2 = min(cols - 1, gx2)
        gy2 = min(rows - 1, gy2)
        if gx2 >= gx1 and gy2 >= gy1:
            grid[gy1:gy2 + 1, gx1:gx2 + 1] = False
    log.info("Pass-through zones applied: %d zones (expand_cells=%d)", len(zones), expand_cells)


def _apply_avoid_zones(grid, zones, min_x, min_y, resolution):
    """Fuerza como obstáculo (True) todas las celdas dentro de cada zona rectangular."""
    if not zones:
        return
    inv_res = 1.0 / resolution
    rows, cols = grid.shape
    log.info("Applying avoid zones on grid of shape: (%d, %d), min_x=%.2f, min_y=%.2f", rows, cols, min_x, min_y)
    for i, zone in enumerate(zones):
        # Support both Pydantic models and dictionary types
        zx1 = getattr(zone, 'x1', None)
        if zx1 is None:
            zx1 = zone.get('x1', 0.0)
            zy1 = zone.get('y1', 0.0)
            zx2 = zone.get('x2', 0.0)
            zy2 = zone.get('y2', 0.0)
        else:
            zy1 = zone.y1
            zx2 = zone.x2
            zy2 = zone.y2
            
        gx1 = int((min(zx1, zx2) - min_x) * inv_res)
        gy1 = int((min(zy1, zy2) - min_y) * inv_res)
        gx2 = int((max(zx1, zx2) - min_x) * inv_res)
        gy2 = int((max(zy1, zy2) - min_y) * inv_res)
        
        log.info("Zone %d original: (%.2f, %.2f) -> (%.2f, %.2f)", i, zx1, zy1, zx2, zy2)
        log.info("Zone %d grid: (%d, %d) -> (%d, %d)", i, gx1, gy1, gx2, gy2)
        
        gx1 = max(0, gx1)
        gy1 = max(0, gy1)
        gx2 = min(cols - 1, gx2)
        gy2 = min(rows - 1, gy2)
        
        if gx2 >= gx1 and gy2 >= gy1:
            grid[gy1:gy2 + 1, gx1:gx2 + 1] = True
            log.info("Zone %d applied to grid range: [%d:%d, %d:%d]", i, gy1, gy2+1, gx1, gx2+1)
        else:
            log.warning("Zone %d skipped due to invalid bounds: gx1=%d, gx2=%d, gy1=%d, gy2=%d", i, gx1, gx2, gy1, gy2)
    log.info("Avoid zones applied: %d zones", len(zones))


def _world_to_grid(x, y, min_x, min_y, resolution):
    return int((x - min_x) / resolution), int((y - min_y) / resolution)


def _grid_to_world(gx, gy, min_x, min_y, resolution):
    return gx * resolution + min_x, gy * resolution + min_y


def _find_nearest_free(grid, gx, gy, max_search=50, allowed_mask=None):
    rows, cols = grid.shape
    if allowed_mask is None:
        allowed_mask = ~grid

    if 0 <= gy < rows and 0 <= gx < cols and allowed_mask[gy, gx]:
        return gx, gy

    for radius in range(1, max_search + 1):
        best = None
        best_dist = float("inf")
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if abs(dx) != radius and abs(dy) != radius:
                    continue
                nx, ny = gx + dx, gy + dy
                if 0 <= ny < rows and 0 <= nx < cols and allowed_mask[ny, nx]:
                    d = dx * dx + dy * dy
                    if d < best_dist:
                        best_dist = d
                        best = (nx, ny)
        if best is not None:
            return best

    return None


def _flood_fill_reachable(grid, start):
    """BFS flood fill desde start. Devuelve mascara de celdas alcanzables."""
    rows, cols = grid.shape
    reachable = np.zeros_like(grid, dtype=bool)
    queue = deque()
    queue.append(start)
    reachable[start[1], start[0]] = True

    while queue:
        cx, cy = queue.popleft()
        for dx, dy in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < cols and 0 <= ny < rows and not grid[ny, nx] and not reachable[ny, nx]:
                reachable[ny, nx] = True
                queue.append((nx, ny))

    return reachable


def _astar(grid, start, goal, dt=None, max_distance=None, resolution=1.0, alpha=1.0, allow_diagonals=True, reuse_grid=None):
    """A* con penalización por cercanía a obstáculos (centrado) y preferencia de 90°.

    dt: distance transform (distancia de cada celda libre al obstáculo más cercano, en celdas).
    alpha: fuerza del centrado. Mayor = más centrado.
    allow_diagonals: si es falso, restringe a movimientos ortogonales (arriba, abajo, izquierda, derecha).
    """
    rows, cols = grid.shape
    sc, sr = start
    gc, gr = goal

    log.info("A* start=(%d,%d) goal=(%d,%d) grid=(%d,%d) alpha=%.2f allow_diagonals=%s", sc, sr, gc, gr, rows, cols, alpha, allow_diagonals)

    if sr < 0 or sr >= rows or sc < 0 or sc >= cols:
        log.error("Start out of bounds")
        return None
    if gr < 0 or gr >= rows or gc < 0 or gc >= cols:
        log.error("Goal out of bounds")
        return None
    if grid[sr, sc]:
        log.error("Start cell is OBSTACLE")
        return None
    if grid[gr, gc]:
        log.error("Goal cell is OBSTACLE")
        return None

    open_set = [(0, 0, sc, sr)]
    g_score = {(sc, sr): 0}
    came_from = {}
    closed = set()
    counter = 1

    neighbors = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if allow_diagonals:
        neighbors.extend([(-1, -1), (-1, 1), (1, -1), (1, 1)])

    while open_set:
        _, _, cx, cy = heapq.heappop(open_set)

        if (cx, cy) in closed:
            continue
        closed.add((cx, cy))

        if cx == gc and cy == gr:
            path = []
            cur = (gc, gr)
            while cur in came_from:
                path.append(cur)
                cur = came_from[cur]
            path.append((sc, sr))
            path.reverse()
            return path

        for dx, dy in neighbors:
            nx, ny = cx + dx, cy + dy
            if nx < 0 or nx >= cols or ny < 0 or ny >= rows:
                continue
            if grid[ny, nx]:
                continue
            if (nx, ny) in closed:
                continue

            is_diag = dx != 0 and dy != 0
            base_cost = 2.0 if is_diag else 1.0

            centering_penalty = 0.0
            if dt is not None:
                dt_val = dt[ny, nx]
                centering_penalty = alpha / (dt_val + 1.0)

            reuse_bonus = 0.0
            if reuse_grid is not None:
                reuse_bonus = -float(reuse_grid[ny, nx]) * 1.5

            tentative_g = g_score[(cx, cy)] + base_cost + centering_penalty + reuse_bonus

            if max_distance and tentative_g * resolution > max_distance:
                continue

            if (nx, ny) not in g_score or tentative_g < g_score[(nx, ny)]:
                g_score[(nx, ny)] = tentative_g
                h = abs(nx - gc) + abs(ny - gr)
                f = tentative_g + h
                came_from[(nx, ny)] = (cx, cy)
                heapq.heappush(open_set, (f, counter, nx, ny))
                counter += 1

    return None


def _smooth_path_grid(path_world, grid, min_x, min_y, resolution):
    """Suavizado usando el grid: verifica que shortcuts no crucen obstáculos."""
    if len(path_world) <= 2:
        return path_world

    rows, cols = grid.shape

    def _line_hits_obstacle(p1, p2):
        x1g = (p1[0] - min_x) / resolution
        y1g = (p1[1] - min_y) / resolution
        x2g = (p2[0] - min_x) / resolution
        y2g = (p2[1] - min_y) / resolution

        dx = x2g - x1g
        dy = y2g - y1g
        dist = (dx * dx + dy * dy) ** 0.5
        steps = max(1, int(dist * 1.5))

        for s in range(1, steps):
            t = s / steps
            cx = int(x1g + dx * t)
            cy = int(y1g + dy * t)
            if 0 <= cy < rows and 0 <= cx < cols and grid[cy, cx]:
                return True
        return False

    smoothed = [path_world[0]]
    i = 0
    while i < len(path_world) - 1:
        best_j = i + 1
        for j in range(len(path_world) - 1, i + 1, -1):
            if not _line_hits_obstacle(path_world[i], path_world[j]):
                best_j = j
                break
        smoothed.append(path_world[best_j])
        i = best_j

    return smoothed


@router.post("/find-path")
async def find_path(req: PathRequest):
    t0 = time.time()
    log.info("=== FIND-PATH REQUEST ===")
    log.info("Origin: (%.2f, %.2f)", req.origin["x"], req.origin["y"])
    log.info("Destination: (%.2f, %.2f)", req.destination["x"], req.destination["y"])
    log.info("Clearance: %.2f, MaxDist: %s", req.clearance, req.max_distance)

    plan = _get_plan_segments(req.plan_id)
    segments = plan["segments"]
    bbox = plan["bbox"]

    if len(segments) == 0:
        raise HTTPException(status_code=400, detail="No hay geometría en el plano")

    waypoints = [req.origin, req.destination]

    routes, best_profile, res = _calculate_all_profiles(
        plan_id=req.plan_id,
        segments=segments,
        bbox=bbox,
        resolution=req.grid_resolution,
        clearance=req.clearance,
        waypoints=waypoints,
        pass_through_zones=req.pass_through_zones,
        avoid_zones=req.avoid_zones,
        max_distance=req.max_distance
    )

    log.info("TOTAL find_path time: %.3f s", time.time() - t0)
    log.info("=== FIND-PATH DONE ===")

    best_data = routes[best_profile]
    return {
        "path": best_data["path"],
        "clearances": best_data["clearances"],
        "distance": best_data["distance"],
        "points": best_data["points"],
        "resolution": res,
        "clearance": req.clearance,
        "routes": routes,
        "best_route_key": best_profile
    }


def _count_turns(path):
    """Cuenta el número de cambios de dirección significativos (>15 grados) en una ruta."""
    if len(path) < 3:
        return 0
    turns = 0
    for i in range(1, len(path) - 1):
        p_prev = path[i-1]
        p_curr = path[i]
        p_next = path[i+1]
        dx1 = p_curr[0] - p_prev[0]
        dy1 = p_curr[1] - p_prev[1]
        dx2 = p_next[0] - p_curr[0]
        dy2 = p_next[1] - p_curr[1]
        
        len1 = (dx1**2 + dy1**2)**0.5
        len2 = (dx2**2 + dy2**2)**0.5
        if len1 < 1e-5 or len2 < 1e-5:
            continue
            
        dot = dx1 * dx2 + dy1 * dy2
        cos_angle = dot / (len1 * len2)
        cos_angle = max(-1.0, min(1.0, cos_angle))
        angle = np.arccos(cos_angle)
        
        if angle > 0.26:  # > 15 grados en radianes
            turns += 1
    return turns


def _calculate_safety_score(min_cl, avg_cl, target_clearance_mm):
    """Calcula un porcentaje de seguridad de la ruta en base a la distancia a las paredes."""
    if min_cl <= 0:
        return 0.0
    
    if target_clearance_mm <= 0:
        return 100.0

    # Relación de la separación mínima con el objetivo del usuario
    min_ratio = min(1.0, min_cl / target_clearance_mm)
    # El promedio de separación suele ser mayor que el mínimo, lo normalizamos con el doble del objetivo
    avg_ratio = min(1.0, avg_cl / (target_clearance_mm * 2.0))
    
    # Penalización extrema si la ruta pasa extremadamente pegada a paredes (<200mm)
    penalty = 1.0
    if min_cl < 200.0:
        penalty = min_cl / 200.0
        
    score = (0.7 * min_ratio + 0.3 * avg_ratio) * 100.0 * penalty
    return round(max(0.0, min(100.0, score)), 1)


def _prepare_grid(plan_id, segments, bbox, resolution, clearance, pass_through_zones=None, avoid_zones=None):
    """Prepara grid, distance transform y dilatación. Reutilizable por find_path y find_path_waypoints."""
    if resolution is None:
        width = bbox["maxX"] - bbox["minX"]
        height = bbox["maxY"] - bbox["minY"]
        total = max(width, height)
        if total <= 0:
            raise HTTPException(status_code=400, detail="Dimensiones del plano inválidas")
        resolution = total / 2000

    clearance_cells = max(1, int(clearance * 1000 / resolution))
    total_dilate = clearance_cells

    seg_lengths = np.sqrt((segments[:, 2] - segments[:, 0]) ** 2 + (segments[:, 3] - segments[:, 1]) ** 2)
    wall_mask = seg_lengths > 2.0
    wall_segments = segments[wall_mask]

    cache_key = f"{plan_id}_{resolution}"
    if cache_key not in _grids_cache:
        grid, min_x, min_y = _build_grid_fast(wall_segments, resolution, bbox)
        if grid is None:
            raise HTTPException(status_code=400, detail="Error construyendo grid")
        _grids_cache[cache_key] = (grid, min_x, min_y, resolution)
    grid, min_x, min_y, res = _grids_cache[cache_key]

    grid_copy = grid.copy()
    if avoid_zones:
        _apply_avoid_zones(grid_copy, avoid_zones, min_x, min_y, res)

    if pass_through_zones:
        _apply_pass_through_zones(grid_copy, pass_through_zones, min_x, min_y, res)
        dilated = _dilate_grid(grid_copy, total_dilate) if total_dilate > 0 else grid_copy.copy()
        if avoid_zones:
            _apply_avoid_zones(dilated, avoid_zones, min_x, min_y, res)
        _apply_pass_through_zones(dilated, pass_through_zones, min_x, min_y, res, expand_cells=total_dilate)
        dt = distance_transform_edt(~dilated)
    else:
        dilated = _dilate_grid(grid_copy, total_dilate) if total_dilate > 0 else grid_copy.copy()
        if avoid_zones:
            _apply_avoid_zones(dilated, avoid_zones, min_x, min_y, res)
        dt = distance_transform_edt(~dilated)

    return grid_copy, dilated, dt, min_x, min_y, res


def _compute_path_segment(dilated, dt, start_free, goal_free, res, max_distance=None, profile="centered", reuse_grid=None):
    """Calcula un tramo de ruta entre dos celdas libres usando el perfil seleccionado. Devuelve (path_grid, clearances_mm)."""
    alpha = 1.0
    allow_diagonals = True
    
    if profile == "shortest":
        alpha = 0.0
    elif profile == "centered":
        alpha = 2.0
    elif profile == "safe":
        alpha = 8.0
    elif profile == "orthogonal":
        alpha = 0.5
        allow_diagonals = False

    path_grid = _astar(dilated, start_free, goal_free, dt=dt, max_distance=max_distance, resolution=res, alpha=alpha, allow_diagonals=allow_diagonals, reuse_grid=reuse_grid)
    if path_grid is None:
        return None, None
    clearances = [round(float(dt[gy, gx]) * res, 2) for gx, gy in path_grid]
    return path_grid, clearances


def _resolve_waypoint_grid_coords(dilated, waypoints, min_x, min_y, res):
    """Ajusta waypoints del mundo a celdas transitables del grid y valida conectividad global."""
    # Obtener la componente conectada libre principal (para evitar atrapar waypoints en huecos aislados)
    free_mask = ~dilated
    labeled_array, num_features = label(free_mask)
    main_free_mask = free_mask
    if num_features > 1:
        component_sizes = np.bincount(labeled_array.ravel())
        if len(component_sizes) > 1:
            largest_label = np.argmax(component_sizes[1:]) + 1
            main_free_mask = (labeled_array == largest_label)

    waypoint_grid_coords = []
    for i, wp in enumerate(waypoints):
        gx, gy = _world_to_grid(wp["x"], wp["y"], min_x, min_y, res)
        free_pt = _find_nearest_free(dilated, gx, gy, allowed_mask=main_free_mask)
        if free_pt is None:
            free_pt = _find_nearest_free(dilated, gx, gy)

        if free_pt is None:
            raise HTTPException(
                status_code=400,
                detail=f"El waypoint {i+1} está dentro de un obstáculo o zona bloqueada."
            )
        waypoint_grid_coords.append(free_pt)

    if waypoint_grid_coords:
        component_labels = {
            int(labeled_array[gy, gx])
            for gx, gy in waypoint_grid_coords
            if 0 <= gy < labeled_array.shape[0] and 0 <= gx < labeled_array.shape[1]
        }
        component_labels.discard(0)
        if len(component_labels) > 1:
            raise HTTPException(
                status_code=400,
                detail="Los waypoints seleccionados no pertenecen a la misma red transitable."
            )

    return waypoint_grid_coords


def _validate_waypoint_path_sequence(dilated, waypoint_grid_coords):
    """Verifica que el orden final elegido sea recorrible entre cada pareja consecutiva."""
    for i in range(len(waypoint_grid_coords) - 1):
        wp_from = waypoint_grid_coords[i]
        wp_to = waypoint_grid_coords[i + 1]
        reachable = _flood_fill_reachable(dilated, wp_from)
        if not reachable[wp_to[1], wp_to[0]]:
            raise HTTPException(
                status_code=400,
                detail=f"No existe ninguna ruta transitable entre el waypoint {i+1} y el waypoint {i+2} debido a muros o estrechamientos."
            )


def _grid_path_distance(path_grid, res):
    """Distancia euclídea del path en unidades del plano."""
    if not path_grid or len(path_grid) < 2:
        return 0.0

    total = 0.0
    for i in range(1, len(path_grid)):
        x0, y0 = path_grid[i - 1]
        x1, y1 = path_grid[i]
        total += math.hypot(x1 - x0, y1 - y0) * res
    return total


def _segment_skip_penalty(path_grid, future_indices, waypoint_grid_coords, res, threshold_mm):
    """Penaliza segmentos que pasan demasiado cerca de waypoints que todavia no toca visitar."""
    if not path_grid or not future_indices or threshold_mm <= 0:
        return 0.0

    threshold_cells = threshold_mm / res
    threshold_sq = threshold_cells * threshold_cells
    penalty = 0.0

    for wp_idx in future_indices:
        wx, wy = waypoint_grid_coords[wp_idx]
        best_sq = min((gx - wx) ** 2 + (gy - wy) ** 2 for gx, gy in path_grid)
        if best_sq <= threshold_sq:
            best_mm = math.sqrt(best_sq) * res
            penalty += (threshold_mm - best_mm) * 8.0

    return penalty


def _build_waypoint_pair_cache(dilated, dt, waypoint_grid_coords, res):
    """Precalcula coste y path shortest entre cada par de waypoints."""
    n = len(waypoint_grid_coords)
    pair_cache = {}

    for i in range(n):
        for j in range(i + 1, n):
            path_grid, _ = _compute_path_segment(
                dilated,
                dt,
                waypoint_grid_coords[i],
                waypoint_grid_coords[j],
                res,
                profile="shortest",
                reuse_grid=None
            )
            if path_grid is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"No existe ninguna ruta transitable entre el waypoint {i+1} y el waypoint {j+1}."
                )

            distance = _grid_path_distance(path_grid, res)
            pair_cache[(i, j)] = {
                "distance": distance,
                "path": path_grid,
            }
            pair_cache[(j, i)] = {
                "distance": distance,
                "path": list(reversed(path_grid)),
            }

    return pair_cache


def _route_total_cost(order, pair_cache, waypoint_grid_coords, res, skip_threshold_mm):
    total = 0.0
    for idx in range(len(order) - 1):
        current_idx = order[idx]
        next_idx = order[idx + 1]
        segment = pair_cache[(current_idx, next_idx)]
        total += segment["distance"]
        total += _segment_skip_penalty(
            segment["path"],
            order[idx + 2:],
            waypoint_grid_coords,
            res,
            skip_threshold_mm
        )
    return total


def _optimize_waypoint_order(waypoints, waypoint_grid_coords, dilated, dt, res, clearance):
    """Optimiza el orden manteniendo fijo el origen y usando coste navegable real."""
    n = len(waypoints)
    if n <= 2:
        return list(range(n))

    skip_threshold_mm = max(res * 2.0, clearance * 1000.0)
    pair_cache = _build_waypoint_pair_cache(dilated, dt, waypoint_grid_coords, res)

    movable_indices = list(range(1, n))
    if len(movable_indices) <= 7:
        best_order = [0] + movable_indices
        best_cost = _route_total_cost(best_order, pair_cache, waypoint_grid_coords, res, skip_threshold_mm)
        for perm in permutations(movable_indices):
            candidate = [0] + list(perm)
            candidate_cost = _route_total_cost(candidate, pair_cache, waypoint_grid_coords, res, skip_threshold_mm)
            if candidate_cost < best_cost:
                best_cost = candidate_cost
                best_order = candidate
        return best_order

    remaining = set(movable_indices)
    order = [0]
    while remaining:
        current = order[-1]
        future = sorted(remaining)
        best_next = min(
            future,
            key=lambda candidate: (
                pair_cache[(current, candidate)]["distance"]
                + _segment_skip_penalty(
                    pair_cache[(current, candidate)]["path"],
                    [idx for idx in future if idx != candidate],
                    waypoint_grid_coords,
                    res,
                    skip_threshold_mm
                ),
                candidate,
            )
        )
        order.append(best_next)
        remaining.remove(best_next)

    improved = True
    while improved:
        improved = False
        for i in range(1, n - 2):
            for j in range(i + 1, n - 1):
                candidate = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if _route_total_cost(candidate, pair_cache, waypoint_grid_coords, res, skip_threshold_mm) < _route_total_cost(order, pair_cache, waypoint_grid_coords, res, skip_threshold_mm):
                    order = candidate
                    improved = True
    return order


def _calculate_all_profiles_from_context(dilated, dt, min_x, min_y, res, clearance, waypoints, waypoint_grid_coords, max_distance=None):
    """Calcula rutas bajo multiples modelos usando un grid ya preparado."""
    _validate_waypoint_path_sequence(dilated, waypoint_grid_coords)

    routes_data = {}
    shortest_dist = float("inf")
    profiles = ["shortest", "centered", "safe", "orthogonal"]

    for profile in profiles:
        all_path_world = []
        all_clearances = []
        waypoint_indices = [0]
        failed = False
        t_start_profile = time.time()

        # Grid to track cells used by earlier segments of the same profile
        reuse_grid = np.zeros(dilated.shape, dtype=np.float32)

        for i in range(len(waypoint_grid_coords) - 1):
            wp_from = waypoint_grid_coords[i]
            wp_to = waypoint_grid_coords[i + 1]

            path_grid, clearances = _compute_path_segment(
                dilated,
                dt,
                wp_from,
                wp_to,
                res,
                max_distance=max_distance,
                profile=profile,
                reuse_grid=reuse_grid if i > 0 else None
            )
            if path_grid is None:
                failed = True
                break

            # Mark path cells in reuse_grid for subsequent segments (5x5 diffusion)
            rows_grid, cols_grid = dilated.shape
            for gx, gy in path_grid:
                r_min, r_max = max(0, gy - 2), min(rows_grid - 1, gy + 2)
                c_min, c_max = max(0, gx - 2), min(cols_grid - 1, gx + 2)
                reuse_grid[r_min:r_max + 1, c_min:c_max + 1] = np.maximum(
                    reuse_grid[r_min:r_max + 1, c_min:c_max + 1], 0.2
                )
                # Inner 3x3 gets stronger signal
                r_min1, r_max1 = max(0, gy - 1), min(rows_grid - 1, gy + 1)
                c_min1, c_max1 = max(0, gx - 1), min(cols_grid - 1, gx + 1)
                reuse_grid[r_min1:r_max1 + 1, c_min1:c_max1 + 1] = np.maximum(
                    reuse_grid[r_min1:r_max1 + 1, c_min1:c_max1 + 1], 0.6
                )
                reuse_grid[gy, gx] = 1.0

            path_world = [_grid_to_world(gx, gy, min_x, min_y, res) for gx, gy in path_grid]

            # Smooth segment if not orthogonal
            if profile != "orthogonal" and len(path_world) > 2:
                path_world = _smooth_path_grid(path_world, dilated, min_x, min_y, res)
                # Recalculate clearances for the smoothed points in this segment
                clearances = []
                for p in path_world:
                    gx, gy = _world_to_grid(p[0], p[1], min_x, min_y, res)
                    rows_dt, cols_dt = dt.shape
                    if 0 <= gy < rows_dt and 0 <= gx < cols_dt:
                        clearances.append(round(float(dt[gy, gx]) * res, 2))
                    else:
                        clearances.append(0.0)

            if i > 0 and all_path_world:
                path_world = path_world[1:]
                clearances = clearances[1:]

            all_path_world.extend(path_world)
            all_clearances.extend(clearances)

            waypoint_indices.append(len(all_path_world) - 1)

        if failed or not all_path_world:
            continue

        t_duration_ms = (time.time() - t_start_profile) * 1000

        smoothed = all_path_world
        smoothed_clearances = all_clearances

        # Calculate final total distance
        if len(smoothed) > 1:
            path_arr = np.array(smoothed, dtype=np.float64)
            diffs = np.diff(path_arr, axis=0)
            total_distance = float(np.sum(np.sqrt(diffs[:, 0] ** 2 + diffs[:, 1] ** 2)))
        else:
            total_distance = 0.0

        min_cl = float(min(smoothed_clearances)) if smoothed_clearances else 0.0
        avg_cl = float(np.mean(smoothed_clearances)) if smoothed_clearances else 0.0
        turns = _count_turns(smoothed)

        routes_data[profile] = {
            "path": [[p[0], p[1]] for p in smoothed],
            "clearances": smoothed_clearances,
            "distance": round(total_distance, 3),
            "points": len(smoothed),
            "waypoint_indices": waypoint_indices,
            "metrics": {
                "min_clearance": round(min_cl, 2),
                "avg_clearance": round(avg_cl, 2),
                "turns": turns,
                "calc_time_ms": round(t_duration_ms, 2)
            }
        }

        if total_distance < shortest_dist:
            shortest_dist = total_distance

    # Calcular puntuaciones y elegir la mejor ruta
    target_clearance_mm = clearance * 1000
    best_profile = "centered"  # Por defecto
    best_score = -1.0

    for prof, data in list(routes_data.items()):
        min_cl = data["metrics"]["min_clearance"]
        avg_cl = data["metrics"]["avg_clearance"]
        dist = data["distance"]

        # Calcular puntuación de seguridad
        safety_score = _calculate_safety_score(min_cl, avg_cl, target_clearance_mm)
        data["metrics"]["safety_score"] = safety_score

        # Calcular puntuación de distancia (comparada con la más corta)
        dist_score = (shortest_dist / dist) * 100.0 if dist > 0 else 0.0
        data["metrics"]["distance_score"] = round(dist_score, 1)

        # Puntuación compuesta
        # Damos un 60% de peso a la seguridad/separación y 40% a la distancia corta.
        critical_penalty = 1.0
        if min_cl < target_clearance_mm * 0.5:
            critical_penalty = 0.3  # Penalización drástica por pasar muy cerca del obstáculo
            
        composite_score = (0.6 * safety_score + 0.4 * dist_score) * critical_penalty
        data["metrics"]["composite_score"] = round(composite_score, 1)

        if composite_score > best_score:
            best_score = composite_score
            best_profile = prof

    # Marcar la recomendada
    for prof, data in routes_data.items():
        data["is_recommended"] = (prof == best_profile)

    return routes_data, best_profile, res


def _calculate_all_profiles(plan_id, segments, bbox, resolution, clearance, waypoints, pass_through_zones=None, avoid_zones=None, max_distance=None):
    """Calcula rutas bajo multiples modelos y genera desgloses tecnicos."""
    _, dilated, dt, min_x, min_y, res = _prepare_grid(
        plan_id,
        segments,
        bbox,
        resolution,
        clearance,
        pass_through_zones=pass_through_zones,
        avoid_zones=avoid_zones
    )
    waypoint_grid_coords = _resolve_waypoint_grid_coords(dilated, waypoints, min_x, min_y, res)
    return _calculate_all_profiles_from_context(
        dilated,
        dt,
        min_x,
        min_y,
        res,
        clearance,
        waypoints,
        waypoint_grid_coords,
        max_distance=max_distance
    )


@router.post("/find-path-waypoints")
async def find_path_waypoints(req: WaypointsRequest):
    t0 = time.time()
    with open("debug_request.txt", "a") as f:
        f.write(f"Waypoints: {req.waypoints}\n")
        f.write(f"Pass-through: {req.pass_through_zones}\n")
        f.write(f"Avoid: {req.avoid_zones}\n\n")
    log.info("=== FIND-PATH-WAYPOINTS REQUEST ===")
    log.info("Waypoints: %d", len(req.waypoints))
    log.info("Pass-through zones: %s", req.pass_through_zones)
    log.info("Avoid zones: %s", req.avoid_zones)

    if len(req.waypoints) < 2:
        raise HTTPException(status_code=400, detail="Se necesitan al menos 2 waypoints")

    plan = _get_plan_segments(req.plan_id)
    segments = plan["segments"]
    bbox = plan["bbox"]

    if len(segments) == 0:
        raise HTTPException(status_code=400, detail="No hay geometría en el plano")

    original_waypoints = list(req.waypoints)
    _, dilated, dt, min_x, min_y, res = _prepare_grid(
        req.plan_id,
        segments,
        bbox,
        req.grid_resolution,
        req.clearance,
        pass_through_zones=req.pass_through_zones,
        avoid_zones=req.avoid_zones
    )
    waypoint_grid_coords = _resolve_waypoint_grid_coords(dilated, original_waypoints, min_x, min_y, res)

    optimized_order = list(range(len(original_waypoints)))
    if req.optimize_order and len(original_waypoints) > 2:
        optimized_order = _optimize_waypoint_order(
            original_waypoints,
            waypoint_grid_coords,
            dilated,
            dt,
            res,
            req.clearance
        )

    waypoints = [original_waypoints[i] for i in optimized_order]
    ordered_waypoint_grid_coords = [waypoint_grid_coords[i] for i in optimized_order]

    routes, best_profile, res = _calculate_all_profiles_from_context(
        dilated,
        dt,
        min_x,
        min_y,
        res,
        req.clearance,
        waypoints,
        ordered_waypoint_grid_coords
    )

    log.info("TOTAL find_path_waypoints time: %.3f s", time.time() - t0)
    log.info("=== FIND-PATH-WAYPOINTS DONE ===")

    best_data = routes[best_profile]
    return {
        "path": best_data["path"],
        "clearances": best_data["clearances"],
        "distance": best_data["distance"],
        "points": best_data["points"],
        "waypoint_indices": best_data["waypoint_indices"],
        "resolution": res,
        "clearance": req.clearance,
        "routes": routes,
        "best_route_key": best_profile,
        "waypoints": waypoints,
        "original_waypoints": original_waypoints,
        "optimized_waypoints": waypoints,
        "optimized_order": optimized_order
    }


@router.post("/analyze-space")
async def analyze_space(req: AnalyzeSpaceRequest):
    if not req.segments:
        raise HTTPException(status_code=400, detail="No hay segmentos para analizar")

    segs = np.array(req.segments, dtype=np.float32)
    min_x = float(segs[:, [0, 2]].min())
    min_y = float(segs[:, [1, 3]].min())
    max_x = float(segs[:, [0, 2]].max())
    max_y = float(segs[:, [1, 3]].max())

    return {
        "bbox": {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y},
        "dimensions": {"width": max_x - min_x, "height": max_y - min_y},
        "total_segments": len(segs),
        "total_length": round(float(np.sum(np.sqrt((segs[:, 2] - segs[:, 0]) ** 2 + (segs[:, 3] - segs[:, 1]) ** 2))), 3),
    }
