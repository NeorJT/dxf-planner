/**
 * DXF Planner - Módulo de Medición
 * Controla la lógica de medición de distancias y áreas
 */

import { state, $ } from "./state.js?v=3";
import { api } from "./api.js?v=5";
import { setStatus, showMeasureResult } from "./ui.js?v=3";

export async function handleMeasureDistClick(world, renderer) {
  state.measurePts.push([world.x, world.y]);
  renderer.setMeasurePoints(state.measurePts);
  
  if (state.measurePts.length === 2) {
    try {
      const result = await api.measure(state.measurePts);
      setStatus(`Distancia: ${result.value.toFixed(4)} ud | Ángulo: ${result.angle_deg.toFixed(1)}°`);
      showMeasureResult(`${result.value.toFixed(4)} ud`);
    } catch (err) {
      console.error("Error al medir distancia:", err);
    }
    state.measurePts = [];
    renderer.setMeasurePoints([]);
    $("measure-tooltip").style.display = "none";
  }
}

export function handleMeasureAreaClick(world, renderer) {
  state.measurePts.push([world.x, world.y]);
  renderer.setMeasurePoints(state.measurePts);
  setStatus(`${state.measurePts.length} puntos — doble click para cerrar`);
}

export async function completeMeasureArea(renderer) {
  if (state.measurePts.length < 3) return;
  try {
    const result = await api.measure(state.measurePts);
    setStatus(`Área: ${result.area.toFixed(4)} ud² | Perímetro: ${result.perimeter.toFixed(4)} ud`);
    showMeasureResult(`Área: ${result.area.toFixed(4)} ud²`);
  } catch (err) {
    console.error("Error al medir área:", err);
  }
  state.measurePts = [];
  renderer.setMeasurePoints([]);
  $("measure-tooltip").style.display = "none";
}
