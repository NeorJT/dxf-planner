"""
DXF Planner - Backend API
Industrial floor plan visualizer and editor with pedestrian route generation
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
import os

import asyncio
from routers import dxf_router, pathfinding_router

app = FastAPI(
    title="DXF Planner API",
    description="API for visualization, editing, and routing on industrial DXF plans",
    version="2.0.0"
)

@app.on_event("startup")
async def startup_event():
    # Start background cleanup task for old plans
    asyncio.create_task(dxf_router.cleanup_expired_plans_loop())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(dxf_router.router, prefix="/api/dxf", tags=["DXF"])
app.include_router(pathfinding_router.router, prefix="/api/pathfinding", tags=["Pathfinding"])

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}

# Servir el frontend en produccion (dist si existe, si no public para dev)
frontend_dir = os.path.join(os.path.dirname(__file__), "../frontend/dist")
if not os.path.exists(frontend_dir):
    frontend_dir = os.path.join(os.path.dirname(__file__), "../frontend/public")
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)