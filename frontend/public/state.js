/**
 * DXF Planner - Módulo de Estado
 * Define el estado global y utilidades básicas
 */

export const state = {
  planId: null,
  filename: null,
  batches: [],
  layers: {},
  entities: [],
  bbox: null,
  origin: { x: 0, y: 0 },

  tool: "pan", // Herramientas válidas: "pan", "select", "measure-dist", "measure-area", "route", "passage"
  isDragging: false,
  dragStart: null,

  measurePts: [],
  selectedEntity: null,

  // Route state
  routeOrigin: null,
  routeDestination: null,
  routePoints: [],
  routeClearances: [],
  routeWidth: 1200,
  routeWaypoints: [],
  routeWaypointIndices: [],
  routes: null,
  activeRouteKey: null,
  routeConfig: {
    clearance: 0.5,
    maxDistance: null,
  },

  // Pass-through zone state
  passThroughZones: [],
  passageDrawingStart: null,

  // Avoid zone state
  avoidZones: [],
  avoidDrawingStart: null,
};

export const $ = id => document.getElementById(id);
