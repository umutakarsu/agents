"""FastAPI app for memscope -- visual inspector for memlayer.

    pip install -e ".[memscope]"
    uvicorn memscope.app:app --reload
    open http://localhost:8000

This slice ships the memory DAG view: per-entity nodes + supersede edges,
rendered as SVG. The other views from the design (Pipeline, Search,
Ingest) are intentionally not built yet.

SECURITY WARNING -- no authentication.
======================================
memscope has no auth layer. Every endpoint trusts a caller-supplied
`principals` query string verbatim, so anyone with network reach to the
listening port can impersonate any principal set (e.g. `group:exec`) and
read restricted content. The ACL pre-filter in memlayer.retrieval is
sound, but the HTTP boundary in this file is *not* an identity boundary
-- it just forwards whatever the caller claimed.

Until a real auth layer is added, bind uvicorn to 127.0.0.1 and treat the
service as single-user. Do NOT expose this port on a shared network.
Specifically:

    uvicorn memscope.app:app --host 127.0.0.1 --port 8000

CORS / CSRF are also unconfigured. Don't run this anywhere a browser tab
from an untrusted origin could reach it."""

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

# Hard ceiling on /api/search k. Without this an attacker can ask for
# millions of rows and DoS the database / fusion loop.
_MAX_SEARCH_K = 100

app = FastAPI(title="memscope", docs_url="/api/docs")


class IngestRequest(BaseModel):
    workspace: str
    dir: str
    allowed_principals: list[str]


def _parse_principals(principals: str) -> list[str]:
    """Comma-separated principals, same shape as /api/search uses."""
    return [p.strip() for p in principals.split(",") if p.strip()]


@app.get("/api/workspaces")
def get_workspaces() -> dict:
    return {"workspaces": workspaces()}


@app.get("/api/entities")
def get_entities(workspace: str, principals: str) -> dict:
    principals_list = _parse_principals(principals)
    return {
        "workspace": workspace,
        "entities": entities(workspace, principals_list),
    }


@app.get("/api/memory/{workspace}/{entity_key:path}")
def get_memory_dag(workspace: str, entity_key: str, principals: str) -> dict:
    principals_list = _parse_principals(principals)
    return memory_dag(workspace, entity_key, principals_list)


@app.get("/api/search")
def get_search(workspace: str, q: str, principals: str, k: int = 10) -> dict:
    principals_list = _parse_principals(principals)
    # Clamp k to [1, _MAX_SEARCH_K]. Otherwise k=999999 hits three large
    # SQL queries and a Python-side fusion over millions of rows.
    k = max(1, min(k, _MAX_SEARCH_K))
    return search_hits(workspace, q, principals_list, k=k)


@app.get("/api/pipeline/stats")
def get_pipeline_stats(workspace: str, principals: str) -> dict:
    principals_list = _parse_principals(principals)
    return pipeline_stats(workspace, principals_list)


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


@app.get("/pitch")
def pitch() -> FileResponse:
    return FileResponse(str(STATIC / "pitch.html"))


@app.get("/board")
def board() -> FileResponse:
    return FileResponse(str(STATIC / "board.html"))


# ---------------------------------------------------------------------------
# Phase 7: identity unification endpoints.
#
# All read endpoints take ``principals`` like the other views so the same
# ACL pre-filter is enforced (an identity is only returned if at least one
# of its memory rows is visible to the caller, or it has no memory rows at
# all). The mutating endpoints (approve / reject) currently trust the
# caller-supplied ``by`` string -- the auth track will replace that with
# the session identity once it lands.
# ---------------------------------------------------------------------------

from memscope.views import (  # noqa: E402  -- intentional append-at-end
    cluster_graph_view,
    identities_for_workspace,
    identity_details,
    merge_proposals_for_workspace,
)
from memlayer.identity import approve as identity_approve  # noqa: E402
from memlayer.identity import reject as identity_reject  # noqa: E402


class ApproveProposalRequest(BaseModel):
    by: str


class RejectProposalRequest(BaseModel):
    reason: str
    by: str


@app.get("/api/identities")
def get_identities(workspace: str, principals: str) -> dict:
    principals_list = _parse_principals(principals)
    return {
        "workspace": workspace,
        "identities": identities_for_workspace(workspace, principals_list),
    }


@app.get("/api/identity/{identity_id}")
def get_identity_endpoint(identity_id: int, principals: str) -> dict:
    principals_list = _parse_principals(principals)
    details = identity_details(identity_id, principals_list)
    if details is None:
        raise HTTPException(status_code=404, detail="identity not found")
    return details


@app.get("/api/identity/{identity_id}/cluster_graph")
def get_cluster_graph(identity_id: int, principals: str) -> dict:
    # principals is currently unused for the cluster graph -- the alias /
    # source-color information isn't access-controlled, only the *content*
    # is. We accept the param so the front-end can call the same shape.
    _ = _parse_principals(principals)
    graph = cluster_graph_view(identity_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="identity not found")
    return graph


@app.get("/api/merge_proposals")
def get_merge_proposals(workspace: str) -> dict:
    return {
        "workspace": workspace,
        "proposals": merge_proposals_for_workspace(workspace),
    }


@app.post("/api/merge_proposals/{proposal_id}/approve")
def post_approve_merge(proposal_id: int, req: ApproveProposalRequest) -> dict:
    try:
        winner_id = identity_approve(proposal_id, by=req.by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "winner_id": winner_id}


@app.post("/api/merge_proposals/{proposal_id}/reject")
def post_reject_merge(proposal_id: int, req: RejectProposalRequest) -> dict:
    try:
        identity_reject(proposal_id, reason=req.reason, by=req.by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}
