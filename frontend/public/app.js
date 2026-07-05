/**
 * DXF Planner - Controlador principal
 * Inicializa la UI, maneja eventos de usuario e interactúa con renderer y api
 */

import { DXFRenderer } from "./renderer.js?v=4";
import { api } from "./api.js?v=5";
import { state, $ } from "./state.js?v=4";
import {
  setStatus,
  setProgress,
  showLoading,
  showProperties,
  renderLayersList
} from "./ui.js?v=4";
import {
  handleMeasureDistClick,
  handleMeasureAreaClick,
  completeMeasureArea
} from "./measure.js?v=4";

let renderer = null;

function init() {
  renderer = new DXFRenderer(canvas);

  $("file-input").addEventListener("change", e => {
    if (e.target.files[0]) loadFile(e.target.files[0]);
  });

  const dropZone = $("drop-zone");
  canvas.parentElement.addEventListener("dragover", e => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });
  canvas.parentElement.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  canvas.parentElement.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    const f = e.dataTransfer.files[0];
    if (f) {
      if (f.name.toLowerCase().endsWith(".dxf")) {
        loadFile(f);
      } else {
        alert("Error: Solo se permiten archivos con extensión .dxf");
      }
    }
  });

  document.querySelectorAll(".tool-btn[data-tool]").forEach(btn => {
    btn.addEventListener("click", () => setTool(btn.dataset.tool));
  });

  $("fit-btn").addEventListener("click", () => renderer.fitToView());
  $("load-local-btn")?.addEventListener("click", () => loadLocalFile("current.dxf"));
  
  // Export dropdown toggling
  const exportBtn = $("export-btn");
  const exportMenu = $("export-menu");
  exportBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    exportMenu.classList.toggle("show");
  });

  document.addEventListener("click", () => {
    exportMenu.classList.remove("show");
  });

  $("export-png-btn").addEventListener("click", () => {
    exportImage("png");
  });

  $("export-dxf-active-btn").addEventListener("click", () => {
    exportDXFWithRoutes(false);
  });

  $("export-dxf-all-btn").addEventListener("click", () => {
    exportDXFWithRoutes(true);
  });

  $("layer-search").addEventListener("input", e => renderLayersList(renderer, e.target.value));

  // Route controls
  $("clear-route-btn")?.addEventListener("click", clearRoute);
  $("calc-route-btn")?.addEventListener("click", calculateWaypointRoute);
  $("route-clearance")?.addEventListener("input", e => {
    $("route-clearance-value").textContent = e.target.value;
  });
  $("route-width")?.addEventListener("input", e => {
    $("route-width-value").textContent = e.target.value;
    if (renderer) {
      renderer.routeWidthMm = parseFloat(e.target.value) * 1000;
      renderer._dirty = true;
    }
  });

  // Zone controls
  $("clear-zones-btn")?.addEventListener("click", clearZones);
  $("clear-avoid-btn")?.addEventListener("click", clearAvoidZones);

  canvas.addEventListener("mousedown", onMouseDown);
  canvas.addEventListener("mousemove", onMouseMove);
  canvas.addEventListener("mouseup", onMouseUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("contextmenu", e => e.preventDefault());
  canvas.addEventListener("dblclick", onDblClick);
  window.addEventListener("keydown", onKeyDown);

  window.addEventListener("beforeunload", () => {
    if (state.planId) {
      api.deletePlanBeacon(state.planId);
    }
  });
}

const canvas = $("gl-canvas");

async function loadFile(file) {
  if (!file || !file.name.toLowerCase().endsWith(".dxf")) {
    alert("Error: Solo se permiten archivos con extensión .dxf");
    return;
  }
  console.log("[loadFile] starting upload:", file.name, file.size, "bytes");
  showLoading(true, "Subiendo archivo...");
  setProgress(10);
  try {
    const data = await api.uploadDXF(file, pct => {
      console.log("[loadFile] progress:", pct.toFixed(1) + "%");
      setProgress(pct);
    });
    console.log("[loadFile] upload complete, processing data");
    setProgress(80);
    await processLoadedData(data, file.name);
  } catch (err) {
    console.error("[loadFile] error:", err);
    setStatus("Error: " + err.message);
    alert("Error cargando el plano:\n" + err.message);
  } finally {
    showLoading(false);
  }
}

async function loadLocalFile(filename) {
  console.log("[loadLocalFile] loading local:", filename);
  showLoading(true, "Cargando current.dxf local...");
  setProgress(10);
  try {
    const data = await api.loadLocalDXF(filename);
    console.log("[loadLocalFile] loaded:", data.stats);
    await processLoadedData(data, filename);
  } catch (err) {
    console.error("[loadLocalFile] error:", err);
    setStatus("Error: " + err.message);
    alert("Error cargando el archivo local:\n" + err.message);
  } finally {
    showLoading(false);
  }
}

async function processLoadedData(data, filename) {
  setProgress(90);
  setStatus("Procesando geometría...");

  state.planId = data.plan_id;
  state.filename = data.filename;
  state.batches = data.batches;
  state.layers = data.layers;
  state.entities = data.entities || [];
  state.bbox = data.bbox;
  state.origin = data.origin || { x: 0, y: 0 };

  renderer.loadBatches(data.batches, data.bbox, state.origin);

  setProgress(100);
  $("drop-zone").classList.add("hidden");
  $("export-btn").disabled = false;
  $("plan-info").style.display = "flex";
  $("plan-name").textContent = filename;
  $("plan-stats").textContent = `${data.stats.total_entities} entidades · ${data.stats.total_batches} batches · ${data.stats.total_segments.toLocaleString()} seg.`;

  renderLayersList(renderer);
  setStatus(`Plano cargado: ${data.stats.total_entities} entidades`);
}

function setTool(tool) {
  console.log("[setTool] switching to:", tool);
  state.tool = tool;
  state.measurePts = [];
  renderer.setMeasurePoints([]);
  $("measure-tooltip").style.display = "none";



  document.querySelectorAll(".tool-btn[data-tool]").forEach(b => {
    b.classList.toggle("active", b.dataset.tool === tool);
  });

  const cursorMap = {
    pan: "cursor-pan",
    select: "cursor-pointer",
    "measure-dist": "cursor-crosshair",
    "measure-area": "cursor-crosshair",
    route: "cursor-crosshair",
    passage: "cursor-crosshair",
    avoid: "cursor-crosshair",
  };
  canvas.className = cursorMap[tool] || "";

  updateToolStatus();
}

function updateToolStatus() {
  const msgs = {
    pan: "Click y arrastra para mover la vista · Scroll para zoom",
    select: "Click en una entidad para seleccionarla",
    "measure-dist": "Click en dos puntos para medir distancia",
    "measure-area": "Click en 3+ puntos · Doble click para cerrar",
    route: "Click para añadir waypoints · Botón para calcular",
    passage: "Arrastra para dibujar zona de paso ( verde )",
    avoid: "Arrastra para dibujar zona a evitar ( roja )",
  };
  setStatus(msgs[state.tool] || "");
}

function onMouseDown(e) {
  if (e.button !== 0) return;
  state.isDragging = false;
  state.dragStart = { x: e.clientX, y: e.clientY };

  if (state.tool === "pan" || e.button === 1) {
    canvas.classList.add("cursor-grabbing");
  }

  if (state.tool === "passage") {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const world = renderer.screenToWorld(sx, sy);
    state.passageDrawingStart = world;
    state.isDragging = true;
  }

  if (state.tool === "avoid") {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const world = renderer.screenToWorld(sx, sy);
    state.avoidDrawingStart = world;
    state.isDragging = true;
  }
}

function onMouseMove(e) {
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const world = renderer ? renderer.screenToWorld(sx, sy) : { x: 0, y: 0 };

  $("coords-display").textContent = `X: ${world.x.toFixed(2)}  Y: ${world.y.toFixed(2)}`;

  if (state.dragStart) {
    const dx = e.clientX - state.dragStart.x;
    const dy = e.clientY - state.dragStart.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) state.isDragging = true;
  }

  if (state.isDragging && state.tool === "pan" && state.dragStart) {
    renderer.pan(e.movementX, e.movementY);
  }

  if (state.tool === "route" && state.dragStart && !state.isDragging) {
    const tip = $("measure-tooltip");
    const dx = world.x - (state.routeOrigin?.x ?? world.x);
    const dy = world.y - (state.routeOrigin?.y ?? world.y);
    const d = Math.hypot(dx, dy).toFixed(3);
    tip.style.display = "block";
    tip.style.left = (e.clientX - rect.left + 12) + "px";
    tip.style.top = (e.clientY - rect.top + 8) + "px";
    tip.textContent = state.routeOrigin ? `D ${d}` : `Origen`;
  }

  if ((state.tool === "measure-dist" || state.tool === "measure-area") && state.measurePts.length > 0) {
    const last = state.measurePts[state.measurePts.length - 1];
    const dx = world.x - last[0];
    const dy = world.y - last[1];
    const d = Math.hypot(dx, dy).toFixed(3);
    const tip = $("measure-tooltip");
    tip.style.display = "block";
    tip.style.left = (e.clientX - rect.left + 12) + "px";
    tip.style.top = (e.clientY - rect.top + 8) + "px";
    tip.textContent = `D ${d}`;
  }

  if (state.tool === "passage" && state.passageDrawingStart) {
    renderer.setPassagePreview(state.passageDrawingStart, world);
  }

  if (state.tool === "avoid" && state.avoidDrawingStart) {
    renderer.setAvoidPreview(state.avoidDrawingStart, world);
  }
}

function onMouseUp(e) {
  if (e.button !== 0) { state.dragStart = null; return; }
  canvas.classList.remove("cursor-grabbing");

  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const world = renderer.screenToWorld(sx, sy);

  console.log("[mouseUp] isDragging:", state.isDragging, "tool:", state.tool);

  if (state.tool === "passage" && state.passageDrawingStart) {
    const start = state.passageDrawingStart;
    const dx = Math.abs(world.x - start.x);
    const dy = Math.abs(world.y - start.y);
    if (dx > 10 || dy > 10) {
      const zone = {
        x1: Math.min(start.x, world.x),
        y1: Math.min(start.y, world.y),
        x2: Math.max(start.x, world.x),
        y2: Math.max(start.y, world.y),
      };
      state.passThroughZones.push(zone);
      renderer.setPassThroughZones(state.passThroughZones);
      updateZoneList();
      setStatus(`Zona de paso #${state.passThroughZones.length} creada.`);
    }
    state.passageDrawingStart = null;
    renderer.clearPassagePreview();
    state.isDragging = false;
    state.dragStart = null;
    return;
  }

  if (state.tool === "avoid" && state.avoidDrawingStart) {
    const start = state.avoidDrawingStart;
    const dx = Math.abs(world.x - start.x);
    const dy = Math.abs(world.y - start.y);
    if (dx > 10 || dy > 10) {
      const zone = {
        x1: Math.min(start.x, world.x),
        y1: Math.min(start.y, world.y),
        x2: Math.max(start.x, world.x),
        y2: Math.max(start.y, world.y),
      };
      state.avoidZones.push(zone);
      renderer.setAvoidZones(state.avoidZones);
      updateAvoidList();
      setStatus(`Zona a evitar #${state.avoidZones.length} creada.`);
    }
    state.avoidDrawingStart = null;
    renderer.clearAvoidPreview();
    state.isDragging = false;
    state.dragStart = null;
    return;
  }

  if (!state.isDragging) {
    handleClick(world);
  }

  state.isDragging = false;
  state.dragStart = null;
}

function onWheel(e) {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  renderer.zoomAt(e.clientX - rect.left, e.clientY - rect.top, -e.deltaY);
}

function onDblClick(e) {
  if (state.tool === "measure-area" && state.measurePts.length >= 3) {
    completeMeasureArea(renderer);
  }
}

function onKeyDown(e) {
  if (e.target.tagName === "INPUT") return;
  const keyMap = {
    "h": "pan",
    "s": "select",
    "d": "measure-dist",
    "a": "measure-area",
    "r": "route",
    "p": "passage",
    "e": "avoid",
  };
  if (e.key.toLowerCase() === "f") { renderer?.fitToView(); return; }
  if (e.key === "Escape") {
    state.measurePts = [];
    renderer.setMeasurePoints([]);
    $("measure-tooltip").style.display = "none";
    setTool("pan");
    return;
  }
  const tool = keyMap[e.key.toLowerCase()];
  if (tool) setTool(tool);
}

async function handleClick(world) {
  console.log("[click] tool:", state.tool, "at", world.x.toFixed(2), world.y.toFixed(2), "isDragging:", state.isDragging);
  if (!renderer || !state.planId) return;

  const tool = state.tool;

  if (tool === "measure-dist") {
    await handleMeasureDistClick(world, renderer);
    return;
  }

  if (tool === "measure-area") {
    handleMeasureAreaClick(world, renderer);
    return;
  }

  if (tool === "select") {
    const nearest = findNearestEntity(world, 10 / renderer.camera.zoom);
    if (nearest) {
      state.selectedEntity = nearest;
      showProperties(nearest);
      setStatus(`Seleccionado: ${nearest.type} — capa: ${nearest.layer}`);
    } else {
      showProperties(null);
    }
    return;
  }

  if (tool === "route") {
    handleRouteClick(world);
    return;
  }
}

async function handleRouteClick(world) {
  console.log("[route] click at", world.x, world.y);
  if (!renderer || !state.planId) return;

  state.routeWaypoints.push({ x: world.x, y: world.y });
  renderer.setWaypoints(state.routeWaypoints, []);
  updateWaypointList();
  setStatus(`Waypoint ${state.routeWaypoints.length} añadido. Click para siguiente o "Calcular ruta".`);
  console.log("[route] waypoint added, total:", state.routeWaypoints.length);
}

async function calculateWaypointRoute() {
  if (state.routeWaypoints.length < 2 || !state.planId) {
    alert("Necesitas al menos 2 waypoints para calcular una ruta.");
    return;
  }

  try {
    const clearance = parseFloat($("route-clearance").value) || 0.5;
    const optimizeOrder = $("route-optimize-order")?.checked || false;

    setStatus("Calculando modelos de ruta comparativos...");
    const result = await api.findPathWaypoints(state.planId, state.routeWaypoints, {
      clearance,
      passThroughZones: state.passThroughZones,
      avoidZones: state.avoidZones,
      optimizeOrder,
    });

    const optimizedWaypoints = result.optimized_waypoints || result.waypoints;
    if (optimizedWaypoints) {
      state.routeWaypoints = optimizedWaypoints;
      updateWaypointList();
    }

    state.routes = result.routes;
    state.activeRouteKey = result.best_route_key;

    const activeRoute = result.routes[result.best_route_key];
    state.routePoints = activeRoute.path;
    state.routeClearances = activeRoute.clearances;

    renderer.setRoutes(result.routes, result.best_route_key);
    renderer.setWaypoints(state.routeWaypoints, activeRoute.waypoint_indices);

    updateRouteComparisonUI();
    setStatus(`Ruta recomendada: ${(activeRoute.distance / 1000).toFixed(2)}m (Modelo: ${getProfileName(result.best_route_key)})`);
  } catch (err) {
    console.error("[calculateWaypointRoute] error:", err);
    setStatus("Error calculando ruta: " + err.message);
    alert("No se pudo calcular la ruta:\n" + err.message);
  }
}

function clearRoute() {
  state.routeOrigin = null;
  state.routeDestination = null;
  state.routePoints = [];
  state.routeClearances = [];
  state.routes = null;
  state.activeRouteKey = null;
  state.routeWaypoints = [];
  state.routeWaypointIndices = [];
  state.avoidZones = [];
  renderer.setAvoidZones([]);
  renderer.clearRoute();
  updateRouteComparisonUI();
  updateWaypointList();
  updateAvoidList();
  setStatus("Ruta limpiada.");
}

function removeZone(index) {
  state.passThroughZones.splice(index, 1);
  renderer.setPassThroughZones(state.passThroughZones);
  updateZoneList();
  setStatus(`Zona #${index + 1} eliminada. ${state.passThroughZones.length} restantes.`);
}

function clearZones() {
  state.passThroughZones = [];
  renderer.setPassThroughZones([]);
  updateZoneList();
  setStatus("Zonas de paso limpiadas.");
}

function updateZoneList() {
  const container = $("zone-list");
  if (!container) return;
  container.innerHTML = "";
  state.passThroughZones.forEach((zone, i) => {
    const div = document.createElement("div");
    div.className = "zone-item";
    div.innerHTML = `
      <span class="zone-num">${i + 1}</span>
      <span class="zone-coords">(${zone.x1.toFixed(0)}, ${zone.y1.toFixed(0)}) → (${zone.x2.toFixed(0)}, ${zone.y2.toFixed(0)})</span>
      <button class="zone-remove" onclick="window._removeZone(${i})">✕</button>
    `;
    container.appendChild(div);
  });
}
window._removeZone = removeZone;

function removeAvoidZone(index) {
  state.avoidZones.splice(index, 1);
  renderer.setAvoidZones(state.avoidZones);
  updateAvoidList();
  setStatus(`Zona a evitar #${index + 1} eliminada. ${state.avoidZones.length} restantes.`);
}

function clearAvoidZones() {
  state.avoidZones = [];
  renderer.setAvoidZones([]);
  updateAvoidList();
  setStatus("Zonas a evitar limpiadas.");
}

function updateAvoidList() {
  const container = $("avoid-list");
  if (!container) return;
  container.innerHTML = "";
  state.avoidZones.forEach((zone, i) => {
    const div = document.createElement("div");
    div.className = "zone-item";
    div.innerHTML = `
      <span class="zone-num">${i + 1}</span>
      <span class="zone-coords">(${zone.x1.toFixed(0)}, ${zone.y1.toFixed(0)}) → (${zone.x2.toFixed(0)}, ${zone.y2.toFixed(0)})</span>
      <button class="zone-remove" onclick="window._removeAvoidZone(${i})">✕</button>
    `;
    container.appendChild(div);
  });
}
window._removeAvoidZone = removeAvoidZone;

function removeWaypoint(index) {
  state.routeWaypoints.splice(index, 1);
  renderer.setWaypoints(state.routeWaypoints, []);
  if (state.routeWaypoints.length < 2) {
    renderer.clearRoute();
    state.routePoints = [];
    state.routeClearances = [];
    state.routes = null;
    state.activeRouteKey = null;
    updateRouteComparisonUI();
  }
  updateWaypointList();
  setStatus(`Waypoint eliminado. ${state.routeWaypoints.length} restantes.`);
}

function getProfileName(key) {
  const names = {
    shortest: "Ruta Más Corta",
    centered: "Ruta Centrada (Equilibrada)",
    safe: "Ruta Segura (Máxima separación)",
    orthogonal: "Ruta Ortogonal (Pasillos / AGV)"
  };
  return names[key] || key;
}

function getProfileDescription(key) {
  const descs = {
    shortest: "Optimiza la distancia al máximo. Es la ruta más directa, aunque puede transitar muy cerca de obstáculos y esquinas.",
    centered: "Mantiene una separación equilibrada con las paredes, buscando un compromiso entre seguridad y longitud.",
    safe: "Maximiza la distancia de seguridad frente a obstáculos y paredes. Ideal para peatones y tránsito general en zonas industriales.",
    orthogonal: "Fuerza trayectorias rectilíneas y giros ortogonales alineados con los pasillos, ideal para vehículos autónomos (AGV/AMR)."
  };
  return descs[key] || "";
}

function updateRouteComparisonUI() {
  const container = $("route-models-list");
  const panel = $("route-comparison-panel");
  if (!container || !panel) return;

  if (!state.routes) {
    panel.style.display = "none";
    return;
  }

  panel.style.display = "block";
  container.innerHTML = "";

  for (const [key, rData] of Object.entries(state.routes)) {
    const isActive = key === state.activeRouteKey;
    const metrics = rData.metrics;
    
    let safetyClass = "safety-high";
    let safetyBarColor = "#10b981"; // Emerald green
    if (metrics.safety_score < 40) {
      safetyClass = "safety-low";
      safetyBarColor = "#ef4444"; // Red
    } else if (metrics.safety_score < 75) {
      safetyClass = "safety-medium";
      safetyBarColor = "#f59e0b"; // Amber
    }

    const card = document.createElement("div");
    card.className = `route-model-card ${isActive ? "active-route" : ""}`;
    card.onclick = () => window._setActiveRoute(key);

    const badgeHtml = rData.is_recommended
      ? `<span class="route-model-badge recommended">⭐ Recomendada</span>`
      : `<span class="route-model-badge secondary">${key === "shortest" ? "Rápida" : key === "orthogonal" ? "AGV" : "Estándar"}</span>`;

    card.innerHTML = `
      <div class="route-model-header">
        <span class="route-model-name ${key}">${getProfileName(key)}</span>
        ${badgeHtml}
      </div>
      <p class="route-model-description">${getProfileDescription(key)}</p>
      <div style="height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; margin: 6px 0; width: 100%;">
        <div style="height: 100%; width: ${metrics.safety_score}%; background: ${safetyBarColor}; transition: width 0.3s ease;"></div>
      </div>
      <div class="route-model-metrics">
        <div class="route-model-metric-item">
          <span class="route-model-metric-label">Distancia:</span>
          <span class="route-model-metric-value">${(rData.distance / 1000).toFixed(1)} m</span>
        </div>
        <div class="route-model-metric-item">
          <span class="route-model-metric-label">Seguridad:</span>
          <span class="route-model-metric-value ${safetyClass}">${metrics.safety_score}%</span>
        </div>
        <div class="route-model-metric-item">
          <span class="route-model-metric-label">Sep. Mín:</span>
          <span class="route-model-metric-value">${(metrics.min_clearance / 1000).toFixed(2)} m</span>
        </div>
        <div class="route-model-metric-item">
          <span class="route-model-metric-label">Giros:</span>
          <span class="route-model-metric-value">${metrics.turns}</span>
        </div>
      </div>
    `;

    container.appendChild(card);
  }
}

window._setActiveRoute = function(key) {
  if (!state.routes || !state.routes[key]) return;
  state.activeRouteKey = key;
  renderer.setActiveRoute(key);
  
  const activeRoute = state.routes[key];
  state.routePoints = activeRoute.path;
  state.routeClearances = activeRoute.clearances;
  
  renderer.setWaypoints(state.routeWaypoints, activeRoute.waypoint_indices);
  
  updateRouteComparisonUI();
  setStatus(`Modelo de ruta activo: ${getProfileName(key)} · Distancia: ${(activeRoute.distance / 1000).toFixed(1)} m`);
};

function updateWaypointList() {
  const container = $("waypoint-list");
  if (!container) return;
  container.innerHTML = "";
  state.routeWaypoints.forEach((wp, i) => {
    const div = document.createElement("div");
    div.className = "waypoint-item";
    
    const upBtn = i > 0 
      ? `<button class="waypoint-btn" onclick="window._moveWaypoint(${i}, -1)" title="Subir">▲</button>` 
      : `<span class="waypoint-btn-placeholder"></span>`;
    const downBtn = i < state.routeWaypoints.length - 1 
      ? `<button class="waypoint-btn" onclick="window._moveWaypoint(${i}, 1)" title="Bajar">▼</button>` 
      : `<span class="waypoint-btn-placeholder"></span>`;
      
    div.innerHTML = `
      <span class="waypoint-num">${i + 1}</span>
      <span class="waypoint-coords">(${wp.x.toFixed(0)}, ${wp.y.toFixed(0)})</span>
      <div class="waypoint-actions">
        ${upBtn}
        ${downBtn}
        <button class="waypoint-remove" onclick="window._removeWaypoint(${i})" title="Eliminar">✕</button>
      </div>
    `;
    container.appendChild(div);
  });
}
window._removeWaypoint = removeWaypoint;

function moveWaypoint(index, direction) {
  const targetIndex = index + direction;
  if (targetIndex < 0 || targetIndex >= state.routeWaypoints.length) return;
  
  // Swap the waypoints
  const temp = state.routeWaypoints[index];
  state.routeWaypoints[index] = state.routeWaypoints[targetIndex];
  state.routeWaypoints[targetIndex] = temp;
  
  // Update the waypoints in renderer
  renderer.setWaypoints(state.routeWaypoints, []);
  updateWaypointList();
  
  // Auto recalculate route if they were already calculated
  if (state.routes) {
    calculateWaypointRoute();
  }
}
window._moveWaypoint = moveWaypoint;

function findNearestEntity(world, threshold) {
  if (!renderer.spatialGrid || !renderer.origin) return null;

  const ox = renderer.origin.x;
  const oy = renderer.origin.y;
  const cellSize = renderer.spatialGridCellSize;

  const key = `${Math.floor(world.x / cellSize)},${Math.floor(world.y / cellSize)}`;

  let best = null;
  let bestDist = threshold * threshold;

  for (let dr = -1; dr <= 1; dr++) {
    for (let dc = -1; dc <= 1; dc++) {
      const k = `${Math.floor(world.x / cellSize) + dc},${Math.floor(world.y / cellSize) + dr}`;
      const cell = renderer.spatialGrid[k];
      if (!cell) continue;
      for (const seg of cell) {
        const x0 = seg.x0 + ox, y0 = seg.y0 + oy;
        const x1 = seg.x1 + ox, y1 = seg.y1 + oy;
        const dist = pointToSegmentDistSq(world, { x: x0, y: y0 }, { x: x1, y: y1 });
        if (dist < bestDist) {
          bestDist = dist;
          best = { id: seg.layer + "_" + Math.round(seg.x0 * 1000), type: "LINE", layer: seg.layer };
        }
      }
    }
  }

  return best;
}

function pointToSegmentDistSq(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return (p.x - a.x) ** 2 + (p.y - a.y) ** 2;
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
  const px = a.x + t * dx - p.x;
  const py = a.y + t * dy - p.y;
  return px * px + py * py;
}

function exportImage(format) {
  if (!renderer || !state.planId) return;
  try {
    setStatus(`Exportando imagen ${format.toUpperCase()}...`);
    const dataUrl = renderer.exportToImage(format);
    const a = document.createElement("a");
    a.href = dataUrl;
    const baseName = state.filename ? state.filename.replace(/\.[^/.]+$/, "") : "plano";
    a.download = `${baseName}_export.${format}`;
    a.click();
    setStatus("Imagen exportada con éxito.");
  } catch (err) {
    console.error("Error al exportar imagen:", err);
    setStatus("Error exportando imagen");
    alert("Error al exportar la imagen:\n" + err.message);
  }
}

async function exportDXFWithRoutes(includeAllRoutes = false) {
  if (!state.planId) return;
  try {
    setStatus("Exportando archivo DXF con rutas...");
    showLoading(true, "Generando archivo DXF...");
    setProgress(30);
    
    const routeData = {
      activeRoute: state.routePoints,
      routes: includeAllRoutes ? state.routes : null,
      waypoints: state.routeWaypoints,
      passThroughZones: state.passThroughZones,
      avoidZones: state.avoidZones,
      filename: state.filename
    };
    
    setProgress(60);
    await api.exportDXF(state.planId, routeData);
    setProgress(100);
    setStatus("Archivo DXF exportado con éxito.");
  } catch (err) {
    console.error("Error al exportar DXF:", err);
    setStatus("Error exportando DXF");
    alert("Error al exportar el archivo DXF:\n" + err.message);
  } finally {
    showLoading(false);
  }
}

init();
