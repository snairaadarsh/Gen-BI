"""
api/workspace_routes.py
------------------------
All /workspace/* onboarding endpoints.

Registers a FastAPI APIRouter that is mounted in api/main.py.
Manages a module-level dict of WorkspaceManager instances keyed by
workspace_id so repeated calls within a session hit the same in-memory state.

FIX #9:  _preload_workspaces() is no longer called on every GET /workspace/list
         request. It runs once at startup (called from main.py lifespan) and
         the in-memory registry is the authoritative source thereafter.

FIX #10: wait_for_ag_chat() replaced the 120-iteration sleep loop with an
         asyncio.Event-based wait so the coroutine yields properly while
         a background file-poller task signals completion.

FIX #24: get_workspace_dir() now calls workspace.manager._workspace_root()
         at call time instead of reading the stale module-level WORKSPACE_ROOT
         alias, so WORKSPACE_ROOT env-var changes after import are respected.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from workspace.manager import WorkspaceManager, _workspace_root

router = APIRouter(prefix="/workspace", tags=["workspace"])

# ── In-process workspace registry ──────────────────────────────────────────────
_managers: dict[str, WorkspaceManager] = {}

# Per-workspace asyncio Events used by wait_for_ag_chat()
_ag_chat_events: dict[str, asyncio.Event] = {}


def _get_workspace_dir(workspace_id: str) -> str:
    """Return the workspace directory, resolving WORKSPACE_ROOT at call time."""
    # FIX #24: Use _workspace_root() (call-time resolution) not the stale
    # module-level WORKSPACE_ROOT alias.
    return os.path.join(_workspace_root(), workspace_id)


def _get_manager(workspace_id: str) -> WorkspaceManager:
    if workspace_id not in _managers:
        mgr = WorkspaceManager.load(workspace_id)
        if mgr is None:
            raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found.")
        _managers[workspace_id] = mgr
    return _managers[workspace_id]


def _preload_workspaces():
    """
    Scan WORKSPACE_ROOT on startup and load every existing workspace into the
    in-memory registry so a server restart never loses track of prior uploads.
    Called once from main.py lifespan — NOT on every list request.
    """
    root = _workspace_root()
    if not os.path.isdir(root):
        return
    loaded = 0
    for name in os.listdir(root):
        wdir = os.path.join(root, name)
        if os.path.isdir(wdir) and os.path.exists(os.path.join(wdir, "meta.json")):
            try:
                mgr = WorkspaceManager.load(name)
                if mgr:
                    _managers[name] = mgr
                    loaded += 1
            except Exception as e:
                print(f"[workspace_routes] Could not reload workspace '{name}': {e}")
    if loaded:
        print(f"[workspace_routes] Pre-loaded {loaded} workspace(s) from {root}")


# ── Request / response models ───────────────────────────────────────────────────

class ConnectDBRequest(BaseModel):
    db_type:  str
    host:     str = ""
    port:     str = ""
    database: str = ""
    username: str = ""
    password: str = ""

class SaveSchemaRequest(BaseModel):
    workspace_id: str
    catalog:      dict
    column_types: Optional[dict] = None

class GenerateGlossaryRequest(BaseModel):
    workspace_id: str
    provider:     str           = "gemini"
    model:        Optional[str] = None

class SaveGlossaryRequest(BaseModel):
    workspace_id: str
    glossary:     list[dict]


# ── AG-Chat async coordination ──────────────────────────────────────────────────
# FIX #10: Replaced the 120-iteration asyncio.sleep polling loop with an
# asyncio.Event. A background task polls the file every second and signals
# the event when the watcher writes "completed" status, so the main coroutine
# yields cleanly while waiting.

async def wait_for_ag_chat(mgr, task: str, model: Optional[str] = None, timeout_seconds: int = 120):
    """
    Write a generate_request.json for the background watcher, then wait
    (non-blockingly) for it to be marked completed or failed.

    Uses an asyncio.Event instead of a polling sleep loop so the ASGI
    event loop is not starved during the wait.

    FIX: Previous version raised a false 504 for watcher-reported failures
    because it only checked file existence (not the status field). Now reads
    the final status and raises an appropriate error for each outcome:
      - "completed" → file deleted, returns normally
      - "failed"    → raises 500 with the watcher's error message
      - still "pending" after timeout → raises 504
    """
    req_file = os.path.join(mgr.workspace_dir, "generate_request.json")
    with open(req_file, "w") as f:
        json.dump({
            "workspace_id": mgr.workspace_id,
            "task": task,
            "provider": "ag_chat",
            "model": model,
            "status": "pending",
        }, f)

    done_event = asyncio.Event()
    _final_status: dict = {}  # mutable container so inner coroutine can write to it

    async def _poll():
        for _ in range(timeout_seconds):
            await asyncio.sleep(1)
            # File removed by watcher → completed and cleaned up
            if not os.path.exists(req_file):
                _final_status["status"] = "completed"
                done_event.set()
                return
            try:
                with open(req_file) as f:
                    data = json.load(f)
                status = data.get("status")
                if status == "completed":
                    try:
                        os.remove(req_file)
                    except OSError:
                        pass
                    _final_status["status"] = "completed"
                    done_event.set()
                    return
                if status == "failed":
                    _final_status["status"] = "failed"
                    _final_status["error"]  = data.get("error", "Unknown watcher error")
                    try:
                        os.remove(req_file)
                    except OSError:
                        pass
                    done_event.set()
                    return
            except (OSError, json.JSONDecodeError):
                pass
        # Timed out — signal the event anyway so the waiter unblocks
        _final_status["status"] = "timeout"
        done_event.set()

    asyncio.create_task(_poll())

    await asyncio.wait_for(done_event.wait(), timeout=timeout_seconds + 1)

    final = _final_status.get("status", "timeout")

    if final == "failed":
        raise HTTPException(
            status_code=500,
            detail=f"AG Chat watcher failed: {_final_status.get('error', 'unknown error')}",
        )

    if final == "timeout":
        # Clean up stale request file
        try:
            os.remove(req_file)
        except OSError:
            pass
        raise HTTPException(
            status_code=504,
            detail=(
                "AG Chat generation timed out after "
                f"{timeout_seconds}s. Make sure the background watcher is running "
                "(watch_workspace.py) and your LLM provider is configured correctly."
            ),
        )


# ── Endpoints ───────────────────────────────────────────────────────────────────

@router.post("/upload-csv")
async def upload_csv(files: list[UploadFile] = File(...)):
    """
    Accept one or more CSV files, create a new workspace, load them into
    a SQLite DB, and return the workspace_id + table summary.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    mgr = WorkspaceManager.create_new()
    _managers[mgr.workspace_id] = mgr

    tmp_dir   = tempfile.mkdtemp()
    tmp_paths = []
    try:
        for upload in files:
            if not upload.filename.lower().endswith(".csv"):
                raise HTTPException(status_code=400, detail=f"'{upload.filename}' is not a CSV file.")
            dest = os.path.join(tmp_dir, upload.filename)
            content = await upload.read()
            with open(dest, "wb") as f:
                f.write(content)
            tmp_paths.append(dest)

        summary = mgr.ingest_csv_files(tmp_paths)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "workspace_id": mgr.workspace_id,
        "status":        mgr.status,
        "tables": {
            tname: {
                "source_file":   info["source_file"],
                "row_count":     info["row_count"],
                "column_names":  list(info["columns"].keys()),
            }
            for tname, info in summary["tables"].items()
        },
    }


@router.post("/connect-db")
async def connect_db(req: ConnectDBRequest, workspace_id: Optional[str] = Query(None)):
    """
    Test an external DB connection and, if successful, create/update the workspace.
    Pass workspace_id to update an existing workspace, or omit to create a new one.
    """
    if workspace_id:
        mgr = _get_manager(workspace_id)
    else:
        mgr = WorkspaceManager.create_new()
        _managers[mgr.workspace_id] = mgr

    try:
        ok, msg = mgr.connect_external_db(
            db_type=req.db_type,
            host=req.host,
            port=req.port,
            database=req.database,
            username=req.username,
            password=req.password,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {
        "workspace_id":  mgr.workspace_id,
        "status":        mgr.status,
        "connected":     ok,
        "message":       msg,
        "table_names":   list(mgr._meta.get("tables", {}).keys()),
    }


@router.post("/infer-schema")
async def infer_schema(
    workspace_id: str = Query(...),
    provider:     str = Query("gemini"),
    model:        Optional[str] = Query(None),
):
    """
    Run LLM schema inference on the ingested workspace data and return the
    generated catalog dict for the frontend schema editor.
    """
    mgr = _get_manager(workspace_id)
    try:
        if provider == "ag_chat":
            await wait_for_ag_chat(mgr, "infer_schema", model=model)
            # Reload meta from disk — the watcher may have updated the status
            mgr._meta = mgr._load_meta()
            catalog = mgr._load_catalog()
        else:
            catalog = mgr.infer_schema(provider=provider, model=model)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=f"Schema inference failed: {e}")

    column_types = {}
    for tname, tinfo in mgr._meta.get("tables", {}).items():
        column_types[tname] = tinfo.get("columns", {})

    return {"workspace_id": workspace_id, "catalog": catalog, "status": mgr.status, "column_types": column_types}


@router.post("/save-schema")
async def save_schema(req: SaveSchemaRequest):
    """Save a user-edited catalog dict back to the workspace."""
    mgr = _get_manager(req.workspace_id)
    try:
        mgr.save_schema(req.catalog)
        if req.column_types:
            for tname, cols in req.column_types.items():
                if tname in mgr._meta.get("tables", {}):
                    mgr._meta["tables"][tname]["columns"] = cols
            mgr._save_meta()
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"workspace_id": req.workspace_id, "status": mgr.status}


@router.post("/generate-glossary")
async def generate_glossary(req: GenerateGlossaryRequest):
    """
    Generate an LLM glossary draft from the workspace catalog.
    Returns a list of { term, definition, maps_to } dicts for the HITL editor.
    """
    mgr = _get_manager(req.workspace_id)
    try:
        if req.provider == "ag_chat":
            await wait_for_ag_chat(mgr, "generate_glossary", model=req.model)
            draft_file = os.path.join(mgr.workspace_dir, "glossary_draft.json")
            if os.path.exists(draft_file):
                with open(draft_file) as f:
                    glossary = json.load(f)
                try:
                    os.remove(draft_file)
                except OSError:
                    pass
            else:
                # Watcher marked completed but did not write the draft — treat as empty
                # This can happen if the catalog had no tables; the frontend can still
                # show an empty glossary editor for manual entry.
                print(f"[workspace_routes] glossary_draft.json missing after ag_chat wait "
                      f"for workspace '{req.workspace_id}' — returning empty glossary.")
                glossary = []
        else:
            glossary = mgr.generate_glossary(provider=req.provider, model=req.model)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=f"Glossary generation failed: {e}")

    return {"workspace_id": req.workspace_id, "glossary": glossary}


@router.post("/save-glossary")
async def save_glossary(req: SaveGlossaryRequest):
    """
    Save the reviewed glossary, rebuild FAISS indexes, and mark the workspace READY.
    After this call the workspace can be used for /ask queries.
    """
    mgr = _get_manager(req.workspace_id)
    try:
        mgr.save_glossary(req.glossary)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Glossary save failed: {e}")

    return {"workspace_id": req.workspace_id, "status": mgr.status}


@router.get("/status")
async def workspace_status(workspace_id: str = Query(...)):
    """Return the current status of a workspace."""
    mgr = _get_manager(workspace_id)
    return mgr.to_status_dict()


@router.get("/list")
async def list_workspaces():
    """
    List all workspaces currently loaded in-memory.

    FIX #9: Removed the _preload_workspaces() call that previously ran on
    every request. Pre-loading happens once at startup via main.py lifespan.
    """
    return [mgr.to_status_dict() for mgr in _managers.values()]


@router.get("/catalog")
async def get_catalog(workspace_id: str = Query(...)):
    """Return the saved catalog of the workspace."""
    mgr = _get_manager(workspace_id)
    column_types = {}
    for tname, tinfo in mgr._meta.get("tables", {}).items():
        column_types[tname] = tinfo.get("columns", {})
    return {
        "workspace_id": workspace_id,
        "catalog": mgr._load_catalog(),
        "column_types": column_types
    }


@router.delete("")
async def delete_workspace(workspace_id: str = Query(...)):
    """
    Reset / delete a workspace. Removes its directory from disk and
    evicts it from the in-memory registry.
    """
    wdir = _get_workspace_dir(workspace_id)
    if os.path.isdir(wdir):
        shutil.rmtree(wdir, ignore_errors=True)
    _managers.pop(workspace_id, None)
    return {"workspace_id": workspace_id, "deleted": True}


# ── Helper used by main.py's /ask endpoint ─────────────────────────────────────

def get_workspace_orchestrator(workspace_id: str):
    """
    Return a workspace-specific Orchestrator for the /ask route.
    Raises 404 if the workspace doesn't exist, 409 if it isn't READY yet.
    """
    mgr = _get_manager(workspace_id)
    if mgr.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Workspace '{workspace_id}' is not ready yet (status: {mgr.status}). "
                "Complete the onboarding flow first."
            ),
        )
    return mgr.get_orchestrator()