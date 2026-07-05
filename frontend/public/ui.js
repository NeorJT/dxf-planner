/**
 * DXF Planner - Módulo de Interfaz de Usuario (UI)
 * Maneja los componentes visuales de la aplicación
 */

import { state, $ } from "./state.js?v=3";

export function setStatus(msg) {
  $("status-msg").textContent = msg;
}

export function setProgress(pct) {
  $("loading-progress").style.width = pct + "%";
}

export function showLoading(show, msg = "") {
  $("loading-overlay").style.display = show ? "flex" : "none";
  if (msg) $("loading-text").textContent = msg;
  if (!show) setProgress(0);
}

export function showMeasureResult(text) {
  const el = $("measure-info");
  el.style.display = "block";
  $("measure-info-content").querySelector("strong").textContent = text;
  $("measure-details").textContent = "";
  setTimeout(() => {
    el.style.display = "none";
  }, 4000);
}

export function showProperties(entity) {
  const panel = $("properties-panel");
  if (!entity) {
    panel.innerHTML = `<p class="empty-hint">Selecciona una entidad</p>`;
    return;
  }
  const rows = Object.entries(entity)
    .filter(([k]) => k !== "id")
    .map(([k, v]) => {
      const val = Array.isArray(v) ? v.map(x => (typeof x === "number" ? x.toFixed(2) : x)).join(", ") : v;
      return `<div class="prop-row"><span class="prop-key">${k}</span><span class="prop-val">${val}</span></div>`;
    }).join("");
  panel.innerHTML = rows;
}

export function renderLayersList(renderer, filter = "") {
  const list = $("layers-list");
  const layers = Object.values(state.layers).filter(l =>
    l.name.toLowerCase().includes(filter.toLowerCase())
  );

  list.innerHTML = layers.map(layer => {
    const isHidden = !layer.visible;
    return `
    <div class="layer-row ${isHidden ? "hidden-layer" : ""}" data-layer="${layer.name}">
      <svg class="layer-eye" viewBox="0 0 24 24" data-layer="${layer.name}">
        ${isHidden
          ? '<path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
          : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>'}
      </svg>
      <div class="layer-color-dot" style="background:${layer.color}"></div>
      <span class="layer-name" title="${layer.name}">${layer.name}</span>
    </div>`;
  }).join("");

  list.querySelectorAll(".layer-eye").forEach(eye => {
    eye.addEventListener("click", e => {
      e.stopPropagation();
      const name = eye.dataset.layer;
      const layer = state.layers[name];
      if (!layer) return;
      layer.visible = !layer.visible;
      renderer.setLayerVisible(name, layer.visible);
      renderLayersList(renderer, filter);
    });
  });
}

