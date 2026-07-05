# DXF Planner — Industrial Floor Plan Visualizer with Pedestrian Routing

🔗 **Demo online:** https://dxf-planner.dxfplanner.workers.dev/

A production-ready web application to visualize and generate pedestrian routes over industrial DXF files.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      FRONTEND                           │
│  HTML + CSS + WebGL (canvas) + Canvas2D overlay         │
│  - Batched rendering (min. draw calls)                  │
│  - Lag-free Pan/Zoom (GPU transformation)               │
│  - Interactive routing and measurement overlays         │
│  └───────────────────┬──────────────────────────────────┘
│                      │ REST API (JSON)
┌───────────────────▼─────────────────────────────────────┐
│                      BACKEND (FastAPI)                  │
│                                                         │
│  /api/dxf/upload      → ezdxf parser → WebGL batches    │
│  /api/pathfinding/    → Grid nav + A* + smoothing       │
│  └──────────────────────────────────────────────────────┘
```

## Installation

### 1. Backend (Python 3.10+)

```bash
# Initialize virtual environment and install dependencies
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run the backend
cd backend
python main.py
```

The server starts at `http://localhost:8000`
API Documentation: `http://localhost:8000/docs`

### 2. Frontend

No build step required — serve static files directly:

```bash
cd frontend/public
# Option A: Python
python -m http.server 3000

# Option B: Node
npx serve . -p 3000
```

Open `http://localhost:3000`

---

## Performance Optimization Techniques

### WebGL Batching
The backend groups all entities of the same color and layer into a single vertex array (`Float32Array`). Instead of N draw calls (one per line), it performs **1 draw call per batch**.
A floor plan with 500,000 lines in 10 colors → 10 draw calls.

### GPU Transformation
Pan and zoom only modify the uniform matrix `u_transform` sent to the vertex shader.
The GPU recalculates the positions of all vertices without resending any data → constant 60fps.

### A* with Clearance
The pathfinding algorithm rasterizes line segments onto a binary grid.
The grid is dilated using `scipy.ndimage.binary_dilation` to maintain a safety margin around walls. Subsequent string-pulling removes redundant waypoints.

---

## Features

### Visualization
- Loads large DXF files up to several hundred MBs
- Smooth zoom/pan at 60fps
- Layer visibility control

### Pedestrian Routing
- Set route waypoints by clicking on the plan
- A* algorithm with configurable clearance from walls
- Automatic route smoothing (string-pulling)
- Compare multiple routing models: Shortest, Centered (Balanced), Safe (Max separation), Orthogonal (AGV paths)
- Custom pass-through zones and avoidance zones
- Waypoint sequence optimization

### Measurement
- Distance between two points (including angle)
- Polygon area calculation (Gauss-Shoelace formula)

---

## Keyboard Shortcuts

| Key | Tool | Description |
|-----|------|-------------|
| H | Pan | Move view |
| S | Select | Select entity to view its layer details |
| D | Measure distance | Measure distance between two points |
| A | Measure area | Click 3+ points and double-click to measure area |
| R | Route | Click to add waypoints to path |
| P | Passage zone | Click and drag to define a pass-through zone |
| E | Avoidance zone | Click and drag to define an avoidance zone |
| F | Fit view | Fit the entire floor plan to the screen |
| Esc | Cancel / Pan | Clear active measurements and switch to Pan tool |

---

## Project Structure

```
dxf-planner/
├── backend/
│   ├── main.py                   # FastAPI application
│   └── routers/
│       ├── dxf_router.py         # Parse DXF → WebGL batches
│       └── pathfinding_router.py # Grid navigation + A* + smoothing
│
├── frontend/
│   └── public/                   # Static files served to the browser
│       ├── index.html
│       ├── style.css
│       ├── renderer.js           # WebGL renderer (batches + 2D overlay)
│       ├── api.js                # FastAPI HTTP client
│       └── app.js                # Main controller (UI + logic)
│
├── requirements.txt              # Backend dependencies
└── README.md
```
