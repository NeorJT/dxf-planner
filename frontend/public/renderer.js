/**
 * WebGL Renderer para DXF Planner v2
 * - Renderizado por batches (minimas draw calls)
 * - Pan/Zoom con transformacion de camara en GPU
 * - Scene origin offset para precision Float32
 * - Capa de anotaciones y rutas superpuesta en Canvas 2D
 * - Spatial index para seleccion de entidades
 */

const VS = `#version 300 es
precision highp float;
uniform mat3 u_transform;
in vec2 a_position;
void main() {
  vec3 pos = u_transform * vec3(a_position, 1.0);
  gl_Position = vec4(pos.xy, 0.0, 1.0);
}`;

const FS = `#version 300 es
precision mediump float;
uniform vec4 u_color;
out vec4 fragColor;
void main() { fragColor = u_color; }`;

export class DXFRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext("webgl2", { antialias: true, alpha: true });
    if (!this.gl) throw new Error("WebGL2 no disponible");

    this._initShader();
    this._initCamera();

    this.batches = [];
    this.overlayBatches = [];
    this.routePoints = null;
    this.routeClearances = null;
    this.routes = null;
    this.activeRouteKey = null;
    this.routeWidthMm = 1200;
    this.routeWaypoints = [];
    this.routeWaypointIndices = [];
    this.routeOrigin = null;
    this.routeDestination = null;
    this.measurePoints = [];
    this.selectedEntityId = null;
    this.hiddenLayers = new Set();
    this.deletedEntityIds = new Set();
    this.spatialGrid = null;
    this.spatialGridCellSize = 0;
    this.passThroughZones = [];
    this.passagePreviewStart = null;
    this.passagePreviewEnd = null;
    this.avoidZones = [];
    this.avoidPreviewStart = null;
    this.avoidPreviewEnd = null;

    this._initOverlayCanvas();

    this._rafId = null;
    this._dirty = true;
    this._loop();

    window.addEventListener("resize", () => this.resize());
    this.resize();
  }

  _initShader() {
    const gl = this.gl;
    const vert = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vert, VS); gl.compileShader(vert);
    const frag = gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(frag, FS); gl.compileShader(frag);
    this.program = gl.createProgram();
    gl.attachShader(this.program, vert);
    gl.attachShader(this.program, frag);
    gl.linkProgram(this.program);
    this.u_transform = gl.getUniformLocation(this.program, "u_transform");
    this.u_color = gl.getUniformLocation(this.program, "u_color");
    this.a_position = gl.getAttribLocation(this.program, "a_position");
  }

  _initCamera() {
    this.camera = { x: 0, y: 0, zoom: 1 };
    this.bbox = null;
    this.origin = { x: 0, y: 0 };
  }

  _initOverlayCanvas() {
    this.overlay = document.createElement("canvas");
    this.overlay.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:2;";
    this.canvas.parentElement.appendChild(this.overlay);
    this.ctx2d = this.overlay.getContext("2d");
  }

  resize() {
    const { canvas, gl, overlay } = this;
    const w = canvas.clientWidth * window.devicePixelRatio;
    const h = canvas.clientHeight * window.devicePixelRatio;
    canvas.width = w; canvas.height = h;
    overlay.width = w; overlay.height = h;
    overlay.style.width = canvas.clientWidth + "px";
    overlay.style.height = canvas.clientHeight + "px";
    gl.viewport(0, 0, w, h);
    this._dirty = true;
  }

  loadBatches(batches, bbox, origin) {
    const gl = this.gl;
    this.batches.forEach(b => { gl.deleteVertexArray(b.vao); gl.deleteBuffer(b.vbo); });
    this.batches = [];

    this.bbox = bbox;
    this.origin = origin || { x: 0, y: 0 };

    // Build spatial index for entity selection
    this._buildSpatialGrid(batches);

    for (const batch of batches) {
      const verts = batch.verts;
      if (!verts || verts.length === 0) continue;

      const vbo = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
      gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);

      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      gl.enableVertexAttribArray(this.a_position);
      gl.vertexAttribPointer(this.a_position, 2, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);

      const [r, g, b] = this._hexToRGB(batch.color || "#CCCCCC");

      this.batches.push({
        vao, vbo,
        count: batch.vertexCount,
        color: [r, g, b, 1],
        layer: batch.layer,
        entityIds: batch.entity_ids || [],
        visible: true,
      });
    }

    this.fitToView();
    this._dirty = true;
  }

  _buildSpatialGrid(batches) {
    if (!this.bbox) return;
    const cellSize = Math.max(
      (this.bbox.maxX - this.bbox.minX) / 100,
      (this.bbox.maxY - this.bbox.minY) / 100,
      0.1
    );
    this.spatialGridCellSize = cellSize;
    this.spatialGrid = {};

    for (const batch of batches) {
      const verts = batch.verts;
      const eids = batch.entity_ids || [];
      const segsPerEntity = eids.length > 0 ? Math.floor((verts.length / 2) / eids.length) : 0;

      // We index at batch+layer level since individual entity mapping could be imprecise
      // Store segment ranges per grid cell for fast lookup
      for (let i = 0; i < verts.length - 3; i += 4) {
        const x0 = verts[i] + this.origin.x;
        const y0 = verts[i + 1] + this.origin.y;
        const x1 = verts[i + 2] + this.origin.x;
        const y1 = verts[i + 3] + this.origin.y;

        const cx = Math.floor(((x0 + x1) / 2) / cellSize);
        const cy = Math.floor(((y0 + y1) / 2) / cellSize);

        const key = `${cx},${cy}`;
        if (!this.spatialGrid[key]) {
          this.spatialGrid[key] = [];
        }
        // Store minimal info for hit testing
        if (this.spatialGrid[key].length < 5) {
          this.spatialGrid[key].push({
            layer: batch.layer,
            color: batch.color,
            x0: verts[i], y0: verts[i + 1],
            x1: verts[i + 2], y1: verts[i + 3],
          });
        }
      }
    }
  }

  addOverlaySegment(x0, y0, x1, y1, color = "#4caf50") {
    const gl = this.gl;
    const ox = this.origin.x, oy = this.origin.y;
    const data = new Float32Array([x0 - ox, y0 - oy, x1 - ox, y1 - oy]);
    const vbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    gl.enableVertexAttribArray(this.a_position);
    gl.vertexAttribPointer(this.a_position, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    const [r, g, b] = this._hexToRGB(color);
    const id = `overlay_${Date.now()}`;
    this.overlayBatches.push({ vao, vbo, count: 2, color: [r, g, b, 1], id, points: [[x0, y0], [x1, y1]] });
    this._dirty = true;
    return id;
  }

  removeOverlay(id) {
    const idx = this.overlayBatches.findIndex(b => b.id === id);
    if (idx !== -1) {
      const b = this.overlayBatches[idx];
      this.gl.deleteVertexArray(b.vao);
      this.gl.deleteBuffer(b.vbo);
      this.overlayBatches.splice(idx, 1);
      this._dirty = true;
    }
  }

  setLayerVisible(layerName, visible) {
    if (visible) this.hiddenLayers.delete(layerName);
    else this.hiddenLayers.add(layerName);
    this._dirty = true;
  }

  setRoute(points, clearances) {
    this.routePoints = points;
    this.routeClearances = clearances || null;
    this.routes = null;
    this.activeRouteKey = null;
    this._dirty = true;
  }

  setRoutes(routes, activeRouteKey) {
    this.routes = routes;
    this.activeRouteKey = activeRouteKey;
    this.routePoints = null;
    this.routeClearances = null;
    this._dirty = true;
  }

  setActiveRoute(key) {
    if (this.routes && this.routes[key]) {
      this.activeRouteKey = key;
      this._dirty = true;
    }
  }

  setWaypoints(waypoints, indices) {
    this.routeWaypoints = waypoints || [];
    this.routeWaypointIndices = indices || [];
    this._dirty = true;
  }

  clearRoute() {
    this.routePoints = null;
    this.routeClearances = null;
    this.routes = null;
    this.activeRouteKey = null;
    this.routeWaypoints = [];
    this.routeWaypointIndices = [];
    this.routeOrigin = null;
    this.routeDestination = null;
    this._dirty = true;
  }

  setRouteOrigin(point) {
    this.routeOrigin = point;
    this._dirty = true;
  }

  setRouteDestination(point) {
    this.routeDestination = point;
    this._dirty = true;
  }

  setMeasurePoints(points) {
    this.measurePoints = points;
    this._dirty = true;
  }

  setPassThroughZones(zones) {
    this.passThroughZones = zones || [];
    this._dirty = true;
  }

  setPassagePreview(start, end) {
    this.passagePreviewStart = start;
    this.passagePreviewEnd = end;
    this._dirty = true;
  }

  clearPassagePreview() {
    this.passagePreviewStart = null;
    this.passagePreviewEnd = null;
    this._dirty = true;
  }

  setAvoidZones(zones) {
    this.avoidZones = zones || [];
    this._dirty = true;
  }

  setAvoidPreview(start, end) {
    this.avoidPreviewStart = start;
    this.avoidPreviewEnd = end;
    this._dirty = true;
  }

  clearAvoidPreview() {
    this.avoidPreviewStart = null;
    this.avoidPreviewEnd = null;
    this._dirty = true;
  }

  markDeleted(entityIds) {
    entityIds.forEach(id => this.deletedEntityIds.add(id));
    this._dirty = true;
  }

  unmarkDeleted(entityIds) {
    entityIds.forEach(id => this.deletedEntityIds.delete(id));
    this._dirty = true;
  }

  fitToView() {
    if (!this.bbox) return;
    const { minX, minY, maxX, maxY } = this.bbox;
    const cw = this.canvas.clientWidth;
    const ch = this.canvas.clientHeight;
    const pw = maxX - minX, ph = maxY - minY;
    if (pw === 0 || ph === 0) return;
    const ar = cw / ch;
    // _buildTransform applies (zoom/ar) to X and zoom to Y, so we need
    // pw * (zoom/ar) <= 2 and ph * zoom <= 2 to fit inside NDC [-1,1].
    const zoom = Math.min((2 * ar) / pw, 2 / ph) * 0.9;
    this.camera = {
      x: (minX + maxX) / 2,
      y: (minY + maxY) / 2,
      zoom,
    };
    this._dirty = true;
  }

  screenToWorld(sx, sy) {
    const cw = this.canvas.clientWidth;
    const ch = this.canvas.clientHeight;
    const ndcX = (sx / cw) * 2 - 1;
    const ndcY = 1 - (sy / ch) * 2;
    const ar = cw / ch;
    return {
      x: this.camera.x + (ndcX * ar) / this.camera.zoom,
      y: this.camera.y + ndcY / this.camera.zoom,
    };
  }

  worldToScreen(wx, wy) {
    const cw = this.canvas.clientWidth;
    const ch = this.canvas.clientHeight;
    const ar = cw / ch;
    const ox = this.origin.x, oy = this.origin.y;
    const nx = wx - ox, ny = wy - oy;
    const ndcX = (nx - this.camera.x + ox) * this.camera.zoom / ar;
    const ndcY = (ny - this.camera.y + oy) * this.camera.zoom;
    return {
      x: ((ndcX + 1) / 2) * cw,
      y: ((1 - ndcY) / 2) * ch,
    };
  }

  pan(dxScreen, dyScreen) {
    const cw = this.canvas.clientWidth;
    const ch = this.canvas.clientHeight;
    const ar = cw / ch;
    this.camera.x -= (dxScreen / cw) * 2 * ar / this.camera.zoom;
    this.camera.y += (dyScreen / ch) * 2 / this.camera.zoom;
    this._dirty = true;
  }

  zoomAt(sx, sy, delta) {
    const before = this.screenToWorld(sx, sy);
    const factor = delta > 0 ? 1.12 : 1 / 1.12;
    let newZoom = this.camera.zoom * factor;
    /* For very large drawings the fit-to-view zoom can be ~1e-6 or smaller.
       Clamp to sane extremes so the user cannot zoom out past the scene or
       in so far that precision collapses. */
    const minZoom = this._minZoom();
    const maxZoom = Math.max(1e6, minZoom * 1e9);
    if (newZoom < minZoom) newZoom = minZoom;
    if (newZoom > maxZoom) newZoom = maxZoom;
    this.camera.zoom = newZoom;
    const after = this.screenToWorld(sx, sy);
    this.camera.x += before.x - after.x;
    this.camera.y += before.y - after.y;
    this._dirty = true;
  }

  /** Minimum useful zoom: fit the whole bbox into the view and allow a bit more zoom-out. */
  _minZoom() {
    if (!this.bbox) return 1e-9;
    const cw = this.canvas.clientWidth;
    const ch = this.canvas.clientHeight;
    const ar = cw / ch;
    const pw = this.bbox.maxX - this.bbox.minX;
    const ph = this.bbox.maxY - this.bbox.minY;
    if (pw === 0 || ph === 0) return 1e-9;
    const fitZoom = Math.min((2 * ar) / pw, 2 / ph);
    return fitZoom * 0.1; // allow zooming out up to 10x the fitted view
  }

  _buildTransform() {
    const cw = this.canvas.width;
    const ch = this.canvas.height;
    const ar = cw / ch;
    const z = this.camera.zoom;
    const cx = this.camera.x, cy = this.camera.y;
    const ox = this.origin.x, oy = this.origin.y;
    // The camera pans in world space, but vertices are in origin-offset space
    // So we apply: world_to_ndc = scale * (vertex - camera_offset)
    // vertex is already (world - origin), camera offset in local = (camera - origin)
    return new Float32Array([
      z / ar, 0,    0,
      0,      z,    0,
      -(cx - ox) * z / ar, -(cy - oy) * z, 1,
    ]);
  }

  _hexToRGB(hex) {
    hex = hex.replace("#", "");
    if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
    const n = parseInt(hex, 16);
    return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
  }

  _loop() {
    const render = () => {
      if (this._dirty) {
        this._render();
        this._dirty = false;
      }
      this._rafId = requestAnimationFrame(render);
    };
    this._rafId = requestAnimationFrame(render);
  }

  _render(clearTransparent = false) {
    const gl = this.gl;
    if (clearTransparent) {
      gl.clearColor(0, 0, 0, 0);
    } else {
      gl.clearColor(0.035, 0.051, 0.071, 1);
    }
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program);
    const transform = this._buildTransform();
    gl.uniformMatrix3fv(this.u_transform, false, transform);

    for (const batch of this.batches) {
      if (!batch.visible || this.hiddenLayers.has(batch.layer)) continue;
      gl.uniform4fv(this.u_color, batch.color);
      gl.bindVertexArray(batch.vao);
      gl.drawArrays(gl.LINES, 0, batch.count);
    }

    for (const batch of this.overlayBatches) {
      gl.uniform4fv(this.u_color, batch.color);
      gl.bindVertexArray(batch.vao);
      gl.drawArrays(gl.LINES, 0, batch.count);
    }

    this._render2DOverlay();
  }

  _render2DOverlay() {
    try {
      const ctx = this.ctx2d;
      const dpr = window.devicePixelRatio;
      ctx.clearRect(0, 0, this.overlay.width, this.overlay.height);
      ctx.save();
      ctx.scale(dpr, dpr);

      const w2s = (wx, wy) => this.worldToScreen(wx, wy);

      // Draw pending waypoints (before route is calculated)
      if (this.routeWaypoints.length > 0 && (!this.routes && (!this.routePoints || this.routePoints.length < 2))) {
        for (let i = 0; i < this.routeWaypoints.length; i++) {
          const wp = this.routeWaypoints[i];
          const p = w2s(wp.x, wp.y);
          ctx.beginPath();
          ctx.arc(p.x, p.y, 10, 0, Math.PI * 2);
          ctx.fillStyle = i === 0 ? "#4caf50" : "#2196f3";
          ctx.fill();
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 2;
          ctx.stroke();
          ctx.fillStyle = "#fff";
          ctx.font = "bold 11px sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(String(i + 1), p.x, p.y);
        }
      }

      // 1. Dibujar rutas inactivas/secundarias si existen (Comentado para evitar ruido visual de bifurcaciones/duplicados)
      /*
      if (this.routes) {
        for (const [key, rData] of Object.entries(this.routes)) {
          if (key === this.activeRouteKey) continue;
          const pts = rData.path;
          if (!pts || pts.length < 2) continue;

          ctx.save();
          ctx.lineCap = "round";
          ctx.lineJoin = "round";
          
          let strokeColor = "rgba(100, 116, 139, 0.4)";
          if (key === "shortest") strokeColor = "rgba(244, 67, 54, 0.35)"; // Rojo atenuado
          else if (key === "centered") strokeColor = "rgba(33, 150, 243, 0.35)"; // Azul atenuado
          else if (key === "safe") strokeColor = "rgba(76, 175, 80, 0.35)"; // Verde atenuado
          else if (key === "orthogonal") strokeColor = "rgba(255, 193, 7, 0.35)"; // Amarillo atenuado
          
          ctx.strokeStyle = strokeColor;
          ctx.lineWidth = 4;
          ctx.setLineDash([4, 6]);

          ctx.beginPath();
          const p0 = w2s(pts[0][0], pts[0][1]);
          ctx.moveTo(p0.x, p0.y);
          for (let j = 1; j < pts.length; j++) {
            const pj = w2s(pts[j][0], pts[j][1]);
            ctx.lineTo(pj.x, pj.y);
          }
          ctx.stroke();
          ctx.restore();
        }
      }
      */

      // 2. Dibujar la ruta activa (ya sea desde routePoints o desde routes[activeRouteKey])
      let activePts = this.routePoints;
      let activeClearances = this.routeClearances;

      if (this.routes && this.activeRouteKey && this.routes[this.activeRouteKey]) {
        activePts = this.routes[this.activeRouteKey].path;
        activeClearances = this.routes[this.activeRouteKey].clearances;
      }

      if (activePts && activePts.length >= 2) {
        const halfWidth = this.routeWidthMm / 2;
        const mmPerPixel = 1 / this.camera.zoom;
        const thicknessPx = (this.routeWidthMm / mmPerPixel) / window.devicePixelRatio;

        ctx.save();
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.lineWidth = Math.max(3, thicknessPx);
        ctx.globalAlpha = 0.85;

        const hasClearances = activeClearances && activeClearances.length === activePts.length;

        for (let i = 0; i < activePts.length - 1; i++) {
          const p1 = w2s(activePts[i][0], activePts[i][1]);
          const p2 = w2s(activePts[i + 1][0], activePts[i + 1][1]);

          let color = "#4caf50";
          if (hasClearances) {
            const cl = activeClearances[i];
            if (cl < halfWidth * 0.5) color = "#f44336"; // Muy estrecho (Rojo)
            else if (cl < halfWidth) color = "#ff9800";  // Algo estrecho (Naranja)
            else color = "#10b981";                      // Seguro (Verde)
          }

          ctx.beginPath();
          ctx.moveTo(p1.x, p1.y);
          ctx.lineTo(p2.x, p2.y);
          ctx.strokeStyle = color;
          ctx.stroke();
        }
        ctx.restore();

        // Dibujar los waypoints numerados para la ruta activa
        if (this.routeWaypoints && this.routeWaypoints.length > 0) {
          for (let i = 0; i < this.routeWaypoints.length; i++) {
            const wp = this.routeWaypoints[i];
            const p = w2s(wp.x, wp.y);
            const isStart = i === 0;
            const isEnd = i === this.routeWaypoints.length - 1;
            const radius = 10;

            ctx.beginPath();
            ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
            ctx.fillStyle = isStart ? "#10b981" : isEnd ? "#ef4444" : "#3b82f6";
            ctx.fill();
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 2;
            ctx.stroke();

            ctx.fillStyle = "#fff";
            ctx.font = "bold 11px sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(String(i + 1), p.x, p.y);
          }
        } else {
          const pStart = w2s(activePts[0][0], activePts[0][1]);
          const pEnd = w2s(activePts[activePts.length - 1][0], activePts[activePts.length - 1][1]);
          ctx.fillStyle = "#10b981";
          ctx.beginPath(); ctx.arc(pStart.x, pStart.y, 7, 0, Math.PI * 2); ctx.fill();
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 2;
          ctx.stroke();
          ctx.fillStyle = "#ef4444";
          ctx.beginPath(); ctx.arc(pEnd.x, pEnd.y, 7, 0, Math.PI * 2); ctx.fill();
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 2;
          ctx.stroke();
        }
      }

      if (this.measurePoints.length > 0) {
        ctx.fillStyle = "#4fc3f7";
        ctx.strokeStyle = "#4fc3f7";
        for (let i = 0; i < this.measurePoints.length; i++) {
          const p = w2s(this.measurePoints[i][0], this.measurePoints[i][1]);
          ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, Math.PI * 2);
          ctx.fill();
          if (i > 0) {
            const prev = w2s(this.measurePoints[i - 1][0], this.measurePoints[i - 1][1]);
            ctx.setLineDash([4, 4]);
            ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.moveTo(prev.x, prev.y); ctx.lineTo(p.x, p.y); ctx.stroke();
            ctx.setLineDash([]);
          }
        }
      }

      // Draw pass-through zones
      for (const zone of this.passThroughZones) {
        const p1 = w2s(zone.x1, zone.y1);
        const p2 = w2s(zone.x2, zone.y2);
        const x = Math.min(p1.x, p2.x);
        const y = Math.min(p1.y, p2.y);
        const w = Math.abs(p2.x - p1.x);
        const h = Math.abs(p2.y - p1.y);

        ctx.fillStyle = "rgba(76, 175, 80, 0.2)";
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = "#4caf50";
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 4]);
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);

        // Label
        ctx.fillStyle = "#4caf50";
        ctx.font = "bold 11px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("PASO", x + w / 2, y + h / 2);
      }

      // Draw passage preview during drawing
      if (this.passagePreviewStart && this.passagePreviewEnd) {
        const p1 = w2s(this.passagePreviewStart.x, this.passagePreviewStart.y);
        const p2 = w2s(this.passagePreviewEnd.x, this.passagePreviewEnd.y);
        const x = Math.min(p1.x, p2.x);
        const y = Math.min(p1.y, p2.y);
        const w = Math.abs(p2.x - p1.x);
        const h = Math.abs(p2.y - p1.y);

        ctx.fillStyle = "rgba(76, 175, 80, 0.15)";
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = "#81c784";
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 4]);
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);
      }

      // Draw avoid zones
      for (const zone of this.avoidZones) {
        const p1 = w2s(zone.x1, zone.y1);
        const p2 = w2s(zone.x2, zone.y2);
        const x = Math.min(p1.x, p2.x);
        const y = Math.min(p1.y, p2.y);
        const w = Math.abs(p2.x - p1.x);
        const h = Math.abs(p2.y - p1.y);

        ctx.fillStyle = "rgba(244, 67, 54, 0.2)";
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = "#f44336";
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 4]);
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);

        // Label
        ctx.fillStyle = "#f44336";
        ctx.font = "bold 11px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("EVITAR", x + w / 2, y + h / 2);
      }

      // Draw avoid preview during drawing
      if (this.avoidPreviewStart && this.avoidPreviewEnd) {
        const p1 = w2s(this.avoidPreviewStart.x, this.avoidPreviewStart.y);
        const p2 = w2s(this.avoidPreviewEnd.x, this.avoidPreviewEnd.y);
        const x = Math.min(p1.x, p2.x);
        const y = Math.min(p1.y, p2.y);
        const w = Math.abs(p2.x - p1.x);
        const h = Math.abs(p2.y - p1.y);

        ctx.fillStyle = "rgba(244, 67, 54, 0.15)";
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = "#e57373";
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 4]);
        ctx.strokeRect(x, y, w, h);
        ctx.setLineDash([]);
      }

      ctx.restore();
    } catch (err) {
      console.error("Error in _render2DOverlay:", err);
    }
  }

  exportToImage(format = "png") {
    const isPNG = format === "png";
    
    // Clear transparent if requested (PNG)
    this._render(isPNG);
 
    const tempCanvas = document.createElement("canvas");
    tempCanvas.width = this.canvas.width;
    tempCanvas.height = this.canvas.height;
    const ctx = tempCanvas.getContext("2d");
 
    ctx.drawImage(this.canvas, 0, 0);
    ctx.drawImage(this.overlay, 0, 0);
 
    // Request a normal render for next frame to restore background color
    this._dirty = true;
 
    const mimeType = isPNG ? "image/png" : "image/jpeg";
    return tempCanvas.toDataURL(mimeType, 0.95);
  }

  destroy() {
    cancelAnimationFrame(this._rafId);
    this.overlay.remove();
  }
}