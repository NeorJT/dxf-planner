/**
 * Cliente API para el backend FastAPI
 * Protocolo v2: ArrayBuffer binario con origin offset para precision Float32
 */

const BASE = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" ? "http://localhost:8000/api" : "/api";
const DEFAULT_UPLOAD_CHUNK_SIZE = 32 * 1024 * 1024;

async function getErrorMessage(res, fallback) {
  try {
    const contentType = res.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const data = await res.json();
      return data?.detail || fallback;
    }
    const text = await res.text();
    return text || fallback;
  } catch {
    return fallback;
  }
}

function parseGeometryResponse(buffer) {
  const view = new DataView(buffer);
  let offset = 0;

  const metaLen = view.getUint32(offset, true); offset += 4;
  const metaBytes = new Uint8Array(buffer, offset, metaLen); offset += metaLen;
  const meta = JSON.parse(new TextDecoder().decode(metaBytes));

  // Skip padding bytes so the header starts at a 4-byte boundary
  const padding = (4 - (offset % 4)) % 4;
  offset += padding;

  const nBatches = view.getUint32(offset, true); offset += 4;
  const batchCounts = [];
  for (let i = 0; i < nBatches; i++) {
    batchCounts.push(view.getUint32(offset, true)); offset += 4;
  }

  const vertsFloats = (buffer.byteLength - offset) / 4;
  const verts = new Float32Array(buffer, offset, vertsFloats);

  const batches = [];
  let cursor = 0;
  for (let i = 0; i < nBatches; i++) {
    const count = batchCounts[i];
    const vertsSlice = new Float32Array(verts.buffer, verts.byteOffset + cursor * 4, count * 2);
    batches.push({
      layer: meta.batch_info[i].layer,
      color: meta.batch_info[i].color,
      verts: vertsSlice,
      vertexCount: count,
      entity_ids: meta.batch_info[i].entity_ids,
    });
    cursor += count * 2;
  }

  return { ...meta, batches };
}

export const api = {
  async uploadDXF(file, onProgress) {
    if (onProgress) onProgress(5);

    const chunkSize = DEFAULT_UPLOAD_CHUNK_SIZE;
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));

    const sessionRes = await fetch(
      `${BASE}/dxf/upload-session?filename=${encodeURIComponent(file.name)}&total_size=${file.size}&total_chunks=${totalChunks}`,
      { method: "POST" }
    );
    if (!sessionRes.ok) {
      throw new Error(await getErrorMessage(sessionRes, "No se pudo iniciar la subida."));
    }
    const { upload_id: uploadId } = await sessionRes.json();

    for (let index = 0; index < totalChunks; index++) {
      const start = index * chunkSize;
      const end = Math.min(file.size, start + chunkSize);
      const chunk = file.slice(start, end);
      let lastError = null;

      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const chunkRes = await fetch(`${BASE}/dxf/upload-chunk/${uploadId}/${index}`, {
            method: "PUT",
            headers: { "Content-Type": "application/octet-stream" },
            body: chunk,
          });
          if (!chunkRes.ok) {
            throw new Error(await getErrorMessage(chunkRes, `Error subiendo bloque ${index + 1}.`));
          }
          lastError = null;
          break;
        } catch (err) {
          lastError = err;
          if (attempt === 3) break;
          await new Promise(resolve => setTimeout(resolve, attempt * 700));
        }
      };

      if (lastError) throw lastError;
      if (onProgress) onProgress(5 + ((index + 1) / totalChunks) * 60);
    }

    if (onProgress) onProgress(68);

    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/dxf/complete-upload/${uploadId}`);
      xhr.responseType = "arraybuffer";
      xhr.onprogress = e => {
        if (e.lengthComputable && onProgress) {
          onProgress(68 + (e.loaded / e.total) * 27);
        }
      };
      xhr.onload = () => {
        if (xhr.status === 200) {
          if (onProgress) onProgress(100);
          try {
            const parsed = parseGeometryResponse(xhr.response);
            resolve(parsed);
          } catch (err) {
            reject(new Error("Error parseando geometria: " + err.message));
          }
        } else {
          try {
            const errView = new Uint8Array(xhr.response);
            const errMsg = JSON.parse(new TextDecoder().decode(errView));
            reject(new Error(errMsg?.detail || "Error al procesar el archivo en el servidor"));
          } catch {
            reject(new Error("Error al procesar el archivo (status " + xhr.status + ")"));
          }
        }
      };
      xhr.onerror = () => reject(new Error("Error de conexion al procesar el archivo"));
      xhr.send();
    });
  },

  deletePlanBeacon(planId) {
    navigator.sendBeacon(`${BASE}/dxf/delete-plan?plan_id=${encodeURIComponent(planId)}`);
  },

  async checkFileExists(filename) {
    const res = await fetch(`${BASE}/dxf/exists?filename=${encodeURIComponent(filename)}`);
    if (!res.ok) return false;
    const data = await res.json();
    return !!data.exists;
  },

  async loadLocalDXF(filename = "current.dxf") {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/dxf/load-local?filename=${encodeURIComponent(filename)}`);
    xhr.responseType = "arraybuffer";
    return new Promise((resolve, reject) => {
      xhr.onload = () => {
        if (xhr.status === 200) {
          try { resolve(parseGeometryResponse(xhr.response)); }
          catch (err) { reject(new Error("Error parseando geometria: " + err.message)); }
        } else {
          reject(new Error("Error cargando archivo local"));
        }
      };
      xhr.onerror = () => reject(new Error("Error de conexion"));
      xhr.send();
    });
  },

  async findPath(planId, origin, destination, options = {}) {
    const res = await fetch(`${BASE}/pathfinding/find-path`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plan_id: planId,
        origin,
        destination,
        clearance: options.clearance ?? 0.5,
        max_distance: options.maxDistance ?? null,
        grid_resolution: options.resolution ?? null,
        pass_through_zones: options.passThroughZones ?? null,
        avoid_zones: options.avoidZones ?? null,
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async findPathWaypoints(planId, waypoints, options = {}) {
    const res = await fetch(`${BASE}/pathfinding/find-path-waypoints`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plan_id: planId,
        waypoints,
        clearance: options.clearance ?? 0.5,
        grid_resolution: options.resolution ?? null,
        pass_through_zones: options.passThroughZones ?? null,
        optimize_order: options.optimizeOrder ?? false,
        avoid_zones: options.avoidZones ?? null,
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async analyzeSpace(segments) {
    const res = await fetch(`${BASE}/pathfinding/analyze-space`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async measure(points) {
    const res = await fetch(`${BASE}/edit/measure`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async deleteEntities(planId, entityIds) {
    const res = await fetch(`${BASE}/edit/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId, entity_ids: entityIds }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async restoreEntities(planId, entityIds) {
    const res = await fetch(`${BASE}/edit/restore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId, entity_ids: entityIds }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async drawLine(planId, start, end, layer = "ANNOTATIONS", color = 3) {
    const res = await fetch(`${BASE}/edit/draw-line`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId, start, end, layer, color }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async addAnnotation(planId, position, text, layer = "ANNOTATIONS") {
    const res = await fetch(`${BASE}/edit/annotate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId, position, text, layer }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    return res.json();
  },

  async exportDXF(planId, data = {}) {
    const res = await fetch(`${BASE}/dxf/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plan_id: planId,
        active_route: data.activeRoute || null,
        routes: data.routes || null,
        waypoints: data.waypoints || null,
        pass_through_zones: data.passThroughZones || null,
        avoid_zones: data.avoidZones || null,
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const baseName = data.filename ? data.filename.replace(/\.[^/.]+$/, "") : "plano";
    a.download = `${baseName}_rutas.dxf`;
    a.click();
    URL.revokeObjectURL(url);
  },
};
