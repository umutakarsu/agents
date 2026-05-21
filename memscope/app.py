"""FastAPI app for memscope -- visual inspector for memlayer.

    pip install -e ".[memscope]"
    uvicorn memscope.app:app --reload
    open http://localhost:8000

This slice ships the memory DAG view: per-entity nodes + supersede edges,
rendered as SVG. The other views from the design (Pipeline, Search,
Ingest) are intentionally not built yet."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from memscope.views import (
    entities,
    memory_dag,
    pipeline_ingest,
    pipeline_stats,
    search_hits,
    workspaces,
)

STATIC = Path(__file__).parent / "static"

# Ingest is allowed only against pre-vetted sample directories so a stray
# request cannot read arbitrary paths off the host.
_ALLOWED_INGEST_DIRS = {"sample_docs", "sample_docs_restricted"}

app = FastAPI(title="memscope", docs_url="/api/docs")


class IngestRequest(BaseModel):
    workspace: str
    dir: str
    allowed_principals: list[str]


@app.get("/api/workspaces")
def get_workspaces() -> dict:
    return {"workspaces": workspaces()}


@app.get("/api/entities")
def get_entities(workspace: str) -> dict:
    return {"workspace": workspace, "entities": entities(workspace)}


@app.get("/api/memory/{workspace}/{entity_key:path}")
def get_memory_dag(workspace: str, entity_key: str) -> dict:
    return memory_dag(workspace, entity_key)


@app.get("/api/search")
def get_search(workspace: str, q: str, principals: str, k: int = 10) -> dict:
    principals_list = [p for p in principals.split(",") if p]
    return search_hits(workspace, q, principals_list, k=k)


@app.get("/api/pipeline/stats")
def get_pipeline_stats(workspace: str) -> dict:
    return pipeline_stats(workspace)


@app.post("/api/pipeline/ingest")
def post_pipeline_ingest(req: IngestRequest) -> dict:
    if req.dir not in _ALLOWED_INGEST_DIRS:
        raise HTTPException(
            status_code=400,
            detail=f"dir must be one of {sorted(_ALLOWED_INGEST_DIRS)}",
        )
    return pipeline_ingest(req.workspace, req.dir, req.allowed_principals)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))
