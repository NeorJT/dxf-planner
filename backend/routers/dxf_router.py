"""
Router para cargar y parsear archivos DXF.
Expone la API REST para la UI del visualizador.
"""

import uuid
import asyncio
import os
import math
import time
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Response, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
import ezdxf
from ezdxf.enums import TextEntityAlignment
from ezdxf.lldxf.const import DXFStructureError

from dxf_parser_ezdxf import (
    parse_dxf_to_render_data,
    serialize_render_data,
    build_pf_segments_fast
)

router = APIRouter()

# Almacenamiento en memoria de los planos cargados
_loaded_plans: dict = {}
_upload_sessions: dict = {}

UPLOAD_CHUNK_SIZE = int(os.getenv("DXF_UPLOAD_CHUNK_SIZE", str(32 * 1024 * 1024)))
MAX_CHUNK_SIZE = int(os.getenv("DXF_MAX_CHUNK_SIZE", str(32 * 1024 * 1024)))
UPLOAD_SESSION_TTL_SECONDS = int(os.getenv("DXF_UPLOAD_SESSION_TTL_SECONDS", "3600"))


def _tmp_root() -> Path:
    root = Path("/tmp")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_dxf_filename(filename: str):
    if not filename.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="Invalid file extension. Only DXF files are allowed.")


def _validate_dxf_file(path: Path):
    if not path.exists():
        raise HTTPException(status_code=404, detail="Uploaded file was not found on the server.")

    if path.stat().st_size < 4:
        raise HTTPException(status_code=400, detail="File is too small to be a valid DXF.")

    with open(path, "rb") as f:
        header_sample = f.read(256).decode("utf-8", errors="ignore").strip()

    if not (
        header_sample.startswith("0")
        or header_sample.startswith("9")
        or "SECTION" in header_sample
        or "$ACADVER" in header_sample
    ):
        raise HTTPException(status_code=400, detail="Invalid file contents. The file does not appear to be a valid DXF.")


async def _process_dxf_file(tmp_path: Path, plan_id: str, filename: str) -> Response:
    _validate_dxf_file(tmp_path)

    render_data = await asyncio.get_event_loop().run_in_executor(
        None, parse_dxf_to_render_data, str(tmp_path)
    )

    render_data["plan_id"] = plan_id
    render_data["filename"] = filename

    pf_segments = build_pf_segments_fast(render_data)
    response_body = serialize_render_data(render_data)

    render_data.pop("plan_id", None)
    render_data.pop("filename", None)
    for b in render_data["batches"]:
        b["verts"] = None

    _loaded_plans[plan_id] = {
        "path": str(tmp_path),
        "filename": filename,
        "segments": pf_segments,
        "bbox": render_data["bbox"],
        "origin": render_data["origin"],
        "layers": render_data["layers"],
        "created_at": time.time(),
    }

    return Response(
        content=response_body,
        media_type="application/octet-stream",
        headers={"X-Geometry-Format": "dxf-planner-v2"}
    )


def _assemble_upload_chunks(session: dict, destination: Path):
    session_dir = Path(session["dir"])
    total_chunks = session["total_chunks"]

    with open(destination, "wb") as out:
        for index in range(total_chunks):
            part_path = session_dir / f"{index}.part"
            if not part_path.exists():
                raise HTTPException(status_code=400, detail=f"Missing upload chunk {index}.")
            with open(part_path, "rb") as part:
                shutil.copyfileobj(part, out, length=1024 * 1024)

    expected_size = session["total_size"]
    actual_size = destination.stat().st_size
    if actual_size != expected_size:
        raise HTTPException(
            status_code=400,
            detail=f"Upload size mismatch. Expected {expected_size} bytes, got {actual_size} bytes."
        )


def _delete_upload_session(upload_id: str):
    session = _upload_sessions.pop(upload_id, None)
    if not session:
        return
    try:
        shutil.rmtree(session["dir"], ignore_errors=True)
    except Exception:
        pass

@router.post("/upload")
async def upload_dxf(request: Request, filename: str):
    _validate_dxf_filename(filename)

    plan_id = str(uuid.uuid4())
    tmp_path = _tmp_root() / f"dxf_{plan_id}.dxf"

    try:
        with open(tmp_path, "wb") as f:
            async for chunk in request.stream():
                if chunk:
                    f.write(chunk)

        return await _process_dxf_file(tmp_path, plan_id, filename)
    except HTTPException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise
    except Exception as e:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-session")
async def create_upload_session(filename: str, total_size: int, total_chunks: int):
    _validate_dxf_filename(filename)

    if total_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid upload size.")
    if total_chunks <= 0:
        raise HTTPException(status_code=400, detail="Invalid chunk count.")

    upload_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    session_dir = _tmp_root() / "dxf_uploads" / upload_id
    session_dir.mkdir(parents=True, exist_ok=False)

    _upload_sessions[upload_id] = {
        "plan_id": plan_id,
        "filename": filename,
        "total_size": total_size,
        "total_chunks": total_chunks,
        "received_chunks": {},
        "dir": str(session_dir),
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    return {
        "upload_id": upload_id,
        "chunk_size": UPLOAD_CHUNK_SIZE,
        "max_chunk_size": MAX_CHUNK_SIZE,
        "expires_in": UPLOAD_SESSION_TTL_SECONDS,
    }


@router.put("/upload-chunk/{upload_id}/{chunk_index}")
async def upload_chunk(upload_id: str, chunk_index: int, request: Request):
    session = _upload_sessions.get(upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found or expired.")
    if chunk_index < 0 or chunk_index >= session["total_chunks"]:
        raise HTTPException(status_code=400, detail="Invalid chunk index.")

    session_dir = Path(session["dir"])
    part_path = session_dir / f"{chunk_index}.part"
    tmp_part_path = session_dir / f"{chunk_index}.part.tmp"
    bytes_written = 0

    try:
        with open(tmp_part_path, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                bytes_written += len(chunk)
                if bytes_written > MAX_CHUNK_SIZE:
                    raise HTTPException(status_code=413, detail="Upload chunk is too large.")
                f.write(chunk)

        tmp_part_path.replace(part_path)
        session["received_chunks"][chunk_index] = bytes_written
        session["updated_at"] = time.time()
        return {"status": "ok", "chunk_index": chunk_index, "bytes": bytes_written}
    except HTTPException:
        if tmp_part_path.exists():
            try:
                tmp_part_path.unlink()
            except Exception:
                pass
        raise
    except Exception as e:
        if tmp_part_path.exists():
            try:
                tmp_part_path.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/complete-upload/{upload_id}")
async def complete_upload(upload_id: str):
    session = _upload_sessions.get(upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found or expired.")

    missing_chunks = [
        index
        for index in range(session["total_chunks"])
        if index not in session["received_chunks"]
    ]
    if missing_chunks:
        raise HTTPException(status_code=400, detail=f"Missing upload chunks: {missing_chunks[:10]}")

    plan_id = session["plan_id"]
    filename = session["filename"]
    tmp_path = _tmp_root() / f"dxf_{plan_id}.dxf"

    try:
        await asyncio.get_event_loop().run_in_executor(
            None, _assemble_upload_chunks, session, tmp_path
        )
        response = await _process_dxf_file(tmp_path, plan_id, filename)
        _delete_upload_session(upload_id)
        return response
    except HTTPException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise
    except Exception as e:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/plan/{plan_id}/layers")
async def get_layers(plan_id: str):
    if plan_id not in _loaded_plans:
        raise HTTPException(status_code=404, detail="Plan no encontrado")
    return {"layers": _loaded_plans[plan_id]["layers"]}

@router.get("/plans")
async def list_plans():
    return {"plans": [
        {"plan_id": k, "filename": v["filename"]}
        for k, v in _loaded_plans.items()
    ]}

@router.get("/exists")
async def check_file_exists(filename: str):
    """Comprueba si un archivo existe en la carpeta del proyecto."""
    project_root = Path(__file__).parent.parent.parent.resolve()
    filepath = (project_root / filename).resolve()
    if not str(filepath).startswith(str(project_root)):
        raise HTTPException(status_code=400, detail="Ruta no permitida")
    return {"exists": filepath.exists()}

@router.post("/load-local")
async def load_local(filename: str = "current.dxf"):
    """Endpoint de desarrollo: carga un archivo DXF local sin upload HTTP."""
    # Prevenir path traversal: solo permitir archivos en el directorio raíz del proyecto
    project_root = Path(__file__).parent.parent.parent.resolve()
    filepath = (project_root / filename).resolve()
    if not str(filepath).startswith(str(project_root)):
        raise HTTPException(status_code=400, detail="Ruta no permitida")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"Archivo no encontrado: {filepath}")

    plan_id = str(uuid.uuid4())
    render_data = await asyncio.get_event_loop().run_in_executor(
        None, parse_dxf_to_render_data, str(filepath)
    )

    render_data["plan_id"] = plan_id
    render_data["filename"] = filename

    pf_segments = build_pf_segments_fast(render_data)
    response_body = serialize_render_data(render_data)

    render_data.pop("plan_id", None)
    render_data.pop("filename", None)
    for b in render_data["batches"]:
        b["verts"] = None

    _loaded_plans[plan_id] = {
        "path": str(filepath),
        "filename": filename,
        "segments": pf_segments,
        "bbox": render_data["bbox"],
        "origin": render_data["origin"],
        "layers": render_data["layers"],
        "created_at": time.time(),
    }

    return Response(
        content=response_body,
        media_type="application/octet-stream",
        headers={"X-Geometry-Format": "dxf-planner-v2"}
    )


class DXFExportWaypoint(BaseModel):
    x: float
    y: float


class DXFExportZone(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class DXFExportRequest(BaseModel):
    plan_id: str
    active_route: Optional[List[List[float]]] = None
    routes: Optional[Dict[str, Any]] = None
    waypoints: Optional[List[DXFExportWaypoint]] = None
    pass_through_zones: Optional[List[DXFExportZone]] = None
    avoid_zones: Optional[List[DXFExportZone]] = None


def remove_file(path: str):
    try:
        os.unlink(path)
    except Exception:
        pass


def _repair_missing_lwpolyline_subclasses(source_path: Path, repaired_path: Path) -> int:
    """Insert missing AcDbPolyline subclass markers in malformed LWPOLYLINE entities."""
    data = source_path.read_bytes()
    newline = "\r\n" if b"\r\n" in data else "\n"
    lines = data.decode("utf-8", errors="surrogateescape").splitlines()
    out: list[str] = []
    repairs = 0
    i = 0

    while i + 1 < len(lines):
        code = lines[i].strip()
        value = lines[i + 1].strip().upper()

        if code == "0" and value == "LWPOLYLINE":
            start = i
            i += 2
            while i + 1 < len(lines) and lines[i].strip() != "0":
                i += 2

            entity = lines[start:i]
            has_polyline_subclass = any(
                entity[j].strip() == "100" and entity[j + 1].strip() == "AcDbPolyline"
                for j in range(0, len(entity) - 1, 2)
            )

            if not has_polyline_subclass:
                polyline_specific_codes = {
                    "90", "70", "43", "38", "39",
                    "10", "20", "40", "41", "42", "91",
                    "210", "220", "230",
                }
                insert_at = len(entity)
                for j in range(2, len(entity) - 1, 2):
                    if entity[j].strip() in polyline_specific_codes:
                        insert_at = j
                        break
                entity[insert_at:insert_at] = ["100", "AcDbPolyline"]
                repairs += 1

            out.extend(entity)
            continue

        out.extend((lines[i], lines[i + 1]))
        i += 2

    if i < len(lines):
        out.append(lines[i])

    repaired_data = (newline.join(out) + newline).encode(
        "utf-8",
        errors="surrogateescape",
    )
    repaired_path.write_bytes(repaired_data)
    return repairs


def _read_dxf_for_export(original_path: Path):
    try:
        return ezdxf.readfile(str(original_path)), None
    except DXFStructureError as exc:
        if "missing 'AcDbPolyline' subclass" not in str(exc):
            raise

        repaired_path = _tmp_root() / f"repaired_{uuid.uuid4().hex}.dxf"
        repairs = _repair_missing_lwpolyline_subclasses(original_path, repaired_path)
        if repairs == 0:
            remove_file(str(repaired_path))
            raise

        return ezdxf.readfile(str(repaired_path)), repaired_path


@router.post("/export")
async def export_dxf(req: DXFExportRequest, background_tasks: BackgroundTasks):
    plan_id = req.plan_id
    if plan_id not in _loaded_plans:
        raise HTTPException(status_code=404, detail="Plan no encontrado o sesión expirada")
    
    plan_info = _loaded_plans[plan_id]
    original_path = Path(plan_info["path"])
    if not original_path.exists():
        raise HTTPException(status_code=404, detail="Archivo original del plano no encontrado en el servidor")
    
    try:
        # Cargar el DXF original en ezdxf
        doc, repaired_path = await asyncio.get_event_loop().run_in_executor(
            None, _read_dxf_for_export, original_path
        )
        if repaired_path:
            background_tasks.add_task(remove_file, str(repaired_path))
        msp = doc.modelspace()
        
        # Calcular escala adaptativa para los waypoints y zonas
        bbox = plan_info["bbox"]
        width = bbox["maxX"] - bbox["minX"]
        height = bbox["maxY"] - bbox["minY"]
        diagonal = math.hypot(width, height)
        
        wp_radius = max(0.5, diagonal * 0.006)
        text_height = max(0.4, diagonal * 0.005)
        
        # 1. Dibujar Zonas a Evitar (color rojo = ACI 1)
        if req.avoid_zones:
            if "ZONAS_EVITAR" not in doc.layers:
                doc.layers.new(name="ZONAS_EVITAR", dxfattribs={"color": 1})
            for zone in req.avoid_zones:
                pts = [
                    (zone.x1, zone.y1),
                    (zone.x2, zone.y1),
                    (zone.x2, zone.y2),
                    (zone.x1, zone.y2),
                    (zone.x1, zone.y1)
                ]
                msp.add_lwpolyline(pts, dxfattribs={"layer": "ZONAS_EVITAR", "color": 1})
                
        # 2. Dibujar Zonas de Paso (color verde claro / cian = ACI 4)
        if req.pass_through_zones:
            if "ZONAS_PASO" not in doc.layers:
                doc.layers.new(name="ZONAS_PASO", dxfattribs={"color": 4})
            for zone in req.pass_through_zones:
                pts = [
                    (zone.x1, zone.y1),
                    (zone.x2, zone.y1),
                    (zone.x2, zone.y2),
                    (zone.x1, zone.y2),
                    (zone.x1, zone.y1)
                ]
                msp.add_lwpolyline(pts, dxfattribs={"layer": "ZONAS_PASO", "color": 4})
                
        # 3. Dibujar Rutas alternativas (si existen y no son la activa)
        route_colors = {
            "shortest": 1,      # Rojo
            "centered": 5,      # Azul
            "safe": 3,          # Verde
            "orthogonal": 2     # Amarillo
        }
        
        active_route_pts = req.active_route
        
        if req.routes:
            if "RUTAS_ALTERNATIVAS" not in doc.layers:
                doc.layers.new(name="RUTAS_ALTERNATIVAS", dxfattribs={"color": 8})
            for key, r_data in req.routes.items():
                pts = r_data.get("path") if isinstance(r_data, dict) else r_data
                if not pts or len(pts) < 2:
                    continue
                if active_route_pts and len(pts) == len(active_route_pts):
                    if abs(pts[0][0] - active_route_pts[0][0]) < 1e-2 and abs(pts[-1][0] - active_route_pts[-1][0]) < 1e-2:
                        continue
                
                color = route_colors.get(key, 8)
                msp.add_lwpolyline(pts, dxfattribs={"layer": "RUTAS_ALTERNATIVAS", "color": color})
                
        # 4. Dibujar Ruta Activa (Capa principal "RUTAS" en Verde = ACI 3)
        if active_route_pts and len(active_route_pts) >= 2:
            if "RUTAS" not in doc.layers:
                doc.layers.new(name="RUTAS", dxfattribs={"color": 3})
            msp.add_lwpolyline(active_route_pts, dxfattribs={"layer": "RUTAS", "color": 3})
            
        # 5. Dibujar Waypoints (Capa "WAYPOINTS" en Amarillo = ACI 2)
        if req.waypoints:
            if "WAYPOINTS" not in doc.layers:
                doc.layers.new(name="WAYPOINTS", dxfattribs={"color": 2})
            for i, wp in enumerate(req.waypoints):
                msp.add_circle((wp.x, wp.y), radius=wp_radius, dxfattribs={"layer": "WAYPOINTS", "color": 2})
                text = msp.add_text(
                    str(i + 1),
                    dxfattribs={
                        "layer": "WAYPOINTS",
                        "color": 2,
                        "height": text_height,
                    }
                )
                text.set_placement((wp.x, wp.y), align=TextEntityAlignment.MIDDLE_CENTER)
                
        # Guardar en un archivo temporal
        tmp_dir = Path("/tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        export_path = tmp_dir / f"export_{plan_id}_{uuid.uuid4().hex}.dxf"
        
        await asyncio.get_event_loop().run_in_executor(
            None, doc.saveas, str(export_path)
        )
        
        background_tasks.add_task(remove_file, str(export_path))
        
        original_filename = Path(plan_info["filename"]).stem
        return FileResponse(
            path=str(export_path),
            filename=f"{original_filename}_rutas.dxf",
            media_type="application/dxf"
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error exportando DXF: {str(e)}")


def delete_plan_by_id(plan_id: str):
    if plan_id in _loaded_plans:
        plan_info = _loaded_plans.pop(plan_id)
        path = plan_info.get("path")
        if path:
            file_path = Path(path)
            try:
                file_path.resolve().relative_to(_tmp_root().resolve())
                if file_path.exists():
                    os.unlink(file_path)
                    print(f"Cleaned up plan file: {path}")
            except ValueError:
                pass
            except Exception as e:
                print(f"Error deleting plan file: {e}")


@router.post("/delete-plan")
async def delete_plan(plan_id: str):
    delete_plan_by_id(plan_id)
    return {"status": "success"}


async def cleanup_expired_plans_loop():
    while True:
        await asyncio.sleep(300)  # Run every 5 minutes
        now = time.time()
        expired_ids = []
        for plan_id, info in list(_loaded_plans.items()):
            # 30 minutes of lifetime
            if now - info.get("created_at", 0) > 1800:
                expired_ids.append(plan_id)
        
        for pid in expired_ids:
            try:
                delete_plan_by_id(pid)
                print(f"Automatic cleanup: expired plan {pid} deleted.")
            except Exception as e:
                print(f"Error in automatic cleanup of plan {pid}: {e}")

        expired_uploads = []
        for upload_id, session in list(_upload_sessions.items()):
            if now - session.get("updated_at", session.get("created_at", 0)) > UPLOAD_SESSION_TTL_SECONDS:
                expired_uploads.append(upload_id)

        for upload_id in expired_uploads:
            try:
                _delete_upload_session(upload_id)
                print(f"Automatic cleanup: expired upload session {upload_id} deleted.")
            except Exception as e:
                print(f"Error in automatic cleanup of upload session {upload_id}: {e}")
