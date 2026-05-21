"""FastAPI app for memscope -- visual inspector for memlayer.

    pip install -e ".[memscope]"
    uvicorn memscope.app:app --reload
    open http://localhost:8000

This slice ships the memory DAG view: per-entity nodes + supersede edges,
rendered as SVG. The other views from the design (Pipeline, Search,
Ingest) are intentionally not built yet."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from memscope.views import entities, memory_dag, workspaces

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="memscope", docs_url="/api/docs")


@app.get("/api/workspaces")
def get_workspaces() -> dict:
    return {"workspaces": workspaces()}


@app.get("/api/entities")
def get_entities(workspace: str) -> dict:
    return {"workspace": workspace, "entities": entities(workspace)}


@app.get("/api/memory/{workspace}/{entity_key:path}")
def get_memory_dag(workspace: str, entity_key: str) -> dict:
    return memory_dag(workspace, entity_key)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))
