"""FastAPI app for memscope -- visual inspector for memlayer.

    pip install -e ".[memscope]"
    uvicorn memscope.app:app --reload
    open http://localhost:8000

Auth model
==========
Closes the P0 from the security audit. The HTTP API used to trust whatever
``principals`` the caller passed in the query string -- anyone with reach to
the listening port could claim ``group:exec`` and read restricted content.
Now:

* Every API endpoint requires ``Authorization: Bearer <token>``.
* The user's principals + allowed workspaces come from the ``users`` row,
  not from the query string. Any ``principals`` value in the URL is
  IGNORED (we log no warning; the field is kept in the URL only because
  removing it would break old bookmarks -- the value is dropped).
* ``workspace`` is checked against the user's allowed workspaces;
  cross-workspace access returns 403.

Anonymous mode
==============
Setting ``MEMSCOPE_AUTH_DISABLED=1`` switches the dependency to a synthetic
"anonymous" user with ``principals=["group:all"]`` and access to every
workspace in the DB. This is the **dev/demo default** -- the bundled demo
flow and story-mode UI run with no token. Production deploys must leave
the variable unset (or set it to ``0``) so auth is required.

Even in anonymous mode, the URL's ``principals`` is ignored: the anonymous
user only has ``group:all``, so a request claiming ``group:exec`` gets the
same restricted-content-invisible answer it would get without auth -- the
attacker's claim doesn't reach the SQL pre-filter.

CORS / CSRF are unconfigured; bind to a host you control."""

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from memlayer.auth import User, authenticate
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

# Anonymous mode: when MEMSCOPE_AUTH_DISABLED is set to "1" (or any truthy
# string), every request acts as this synthetic user. group:all is the
# least-privileged principal -- the anonymous user CANNOT see anything
# restricted to e.g. group:exec, even if they pass ?principals=group:exec.
_ANON_USER = User(
    id=0,
    email="anonymous@local",
    principals=["group:all"],
    workspaces=[],  # populated lazily from the DB; see _maybe_anon().
    source_type="agent",
)


def _auth_disabled() -> bool:
    return os.environ.get("MEMSCOPE_AUTH_DISABLED", "").strip() in {"1", "true", "yes"}


def current_user(
    authorization: str | None = Header(default=None),
) -> User:
    """FastAPI dependency. Returns the authenticated user, or the synthetic
    anonymous user when MEMSCOPE_AUTH_DISABLED is on. Raises 401 otherwise."""
    if _auth_disabled():
        # In anonymous mode the user gets access to every workspace the DB
        # knows about. We resolve this here (not at import time) so newly
        # ingested workspaces show up without a restart.
        return User(
            id=_ANON_USER.id,
            email=_ANON_USER.email,
            principals=list(_ANON_USER.principals),
            workspaces=workspaces(),
            source_type=_ANON_USER.source_type,
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing Authorization: Bearer <token>"
        )
    token = authorization[len("Bearer "):]
    user = authenticate(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


def _require_workspace(user: User, workspace: str) -> None:
    """Reject requests for a workspace the user doesn't own. 403 (not 404)
    so the response shape doesn't leak workspace existence to an
    unauthorized caller -- they already know it exists because they named
    it; 403 is the honest answer."""
    if workspace not in user.workspaces:
        raise HTTPException(
            status_code=403,
            detail=f"User not authorized for workspace {workspace!r}",
        )


app = FastAPI(title="memscope", docs_url="/api/docs")


class IngestRequest(BaseModel):
    workspace: str
    dir: str
    allowed_principals: list[str]


@app.get("/api/whoami")
def get_whoami(user: User = Depends(current_user)) -> dict:
    """Cheap endpoint the frontend uses to discover the signed-in identity
    (or confirm anonymous mode) so the topbar can show an email chip."""
    return {
        "email": user.email,
        "principals": user.principals,
        "workspaces": user.workspaces,
        "source_type": user.source_type,
        "anonymous": _auth_disabled(),
    }


@app.get("/api/workspaces")
def get_workspaces(user: User = Depends(current_user)) -> dict:
    """Intersect what the DB has with what the user is allowed to see. A
    workspace they don't own is invisible -- they can't enumerate tenants."""
    all_ws = set(workspaces())
    visible = sorted(all_ws.intersection(user.workspaces))
    return {"workspaces": visible}


@app.get("/api/entities")
def get_entities(
    workspace: str,
    user: User = Depends(current_user),
) -> dict:
    _require_workspace(user, workspace)
    return {
        "workspace": workspace,
        "entities": entities(workspace, user.principals),
    }


@app.get("/api/memory/{workspace}/{entity_key:path}")
def get_memory_dag(
    workspace: str,
    entity_key: str,
    user: User = Depends(current_user),
) -> dict:
    _require_workspace(user, workspace)
    return memory_dag(workspace, entity_key, user.principals)


@app.get("/api/search")
def get_search(
    workspace: str,
    q: str,
    k: int = 10,
    user: User = Depends(current_user),
) -> dict:
    _require_workspace(user, workspace)
    # Clamp k to [1, _MAX_SEARCH_K]. Otherwise k=999999 hits three large
    # SQL queries and a Python-side fusion over millions of rows.
    k = max(1, min(k, _MAX_SEARCH_K))
    return search_hits(workspace, q, user.principals, k=k)


@app.get("/api/pipeline/stats")
def get_pipeline_stats(
    workspace: str,
    user: User = Depends(current_user),
) -> dict:
    _require_workspace(user, workspace)
    return pipeline_stats(workspace, user.principals)


@app.post("/api/pipeline/ingest")
def post_pipeline_ingest(
    req: IngestRequest,
    user: User = Depends(current_user),
) -> dict:
    _require_workspace(user, req.workspace)
    if req.dir not in _ALLOWED_INGEST_DIRS:
        raise HTTPException(
            status_code=400,
            detail=f"dir must be one of {sorted(_ALLOWED_INGEST_DIRS)}",
        )
    # Tighten allowed_principals to the intersection of what the caller asked
    # for and what they themselves own. An agent service-account can't ingest
    # a doc visible to a group it doesn't belong to.
    requested = set(req.allowed_principals)
    owned = set(user.principals)
    effective = sorted(requested.intersection(owned))
    if not effective:
        raise HTTPException(
            status_code=403,
            detail=(
                "allowed_principals must be a subset of the caller's principals "
                f"({sorted(owned)})"
            ),
        )
    return pipeline_ingest(req.workspace, req.dir, effective)


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
