"""
api/main.py
-----------
FastAPI application exposing the GenBI agent chain via /ask and the
/workspace/* onboarding endpoints.

Run: uvicorn api.main:app --reload
     (from the project root)

FIX #3:  CORS allowed origins now read from CORS_ALLOWED_ORIGINS env var
         (comma-separated). Defaults to ["http://localhost:3000",
         "http://localhost:8002"] for local dev. Set "*" explicitly in .env
         only if you understand the security implications.

FIX #8:  _default_orchestrator is now initialised inside the lifespan
         startup handler (not at module import time) so startup errors are
         reported clearly rather than as opaque import failures.

FIX #20: Replaced deprecated @app.on_event("startup") with the modern
         asynccontextmanager lifespan pattern (FastAPI >= 0.93).

FIX #2:  Removed Windows-only netstat/taskkill shell commands from startup.
         The background watcher process should be managed externally (e.g.
         via a process manager or docker-compose) — not by the API server.

FIX #17: Moved all standard-library imports to module level.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import subprocess
import urllib.request
from contextlib import asynccontextmanager
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from agents.orchestrator import Orchestrator
from api.workspace_routes import router as workspace_router, get_workspace_orchestrator, _preload_workspaces

# ── CORS configuration ────────────────────────────────────────────────────────
# FIX #3: Read allowed origins from environment; no wildcard by default.
_raw_origins = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8002",
)
CORS_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]




# ── Lifespan (replaces deprecated @app.on_event) ─────────────────────────────
# FIX #20: Use asynccontextmanager lifespan instead of on_event("startup").
# FIX #8:  Orchestrator is built here, not at module import time, so errors
#           surface with a clear startup message instead of an opaque crash.
# FIX #2:  No Windows-specific shell commands. The background watcher is
#           launched here as a simple subprocess; manage it with an external
#           process supervisor for production deployments.

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-load existing workspaces from disk
    _preload_workspaces()

    # Launch background workspace watcher (cross-platform)
    watcher_process = None
    script_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "watch_workspace.py")
    )
    if os.path.exists(script_path):
        print(f"[Startup] Launching workspace watcher: {script_path}")
        watcher_process = subprocess.Popen(
            [sys.executable, "-u", script_path],
            stdin=subprocess.PIPE,
        )
    else:
        print("[Startup] watch_workspace.py not found — skipping watcher launch.")

    yield  # application runs here

    # Shutdown: terminate watcher if we started it
    if watcher_process and watcher_process.poll() is None:
        watcher_process.terminate()
        try:
            watcher_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watcher_process.kill()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="GenBI API",
    description=(
        "Multi-agent Business Intelligence system — "
        "answers natural-language questions against any dataset."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

# FIX #3: Restricted CORS origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount UI
ui_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ui"))
if os.path.exists(ui_dir):
    app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")

# Register workspace router
app.include_router(workspace_router)


# ── Request / Response models ─────────────────────────────────────────────────

class AskRequest(BaseModel):
    question:     str
    provider:     str           = "gemini"
    model:        Optional[str] = None
    session_id:   Optional[str] = None
    workspace_id: Optional[str] = None


class ChartSpec(BaseModel):
    chart_type:   str           = "table"
    title:        str           = ""
    x_key:        Optional[str] = None
    y_key:        Optional[str] = None
    series_label: Optional[str] = None


class AskResponse(BaseModel):
    answer:               str
    source:               str
    sql_used:             Optional[str]       = None
    rows_returned:        int                 = 0
    rows_data:            list[dict]          = []
    chart_spec:           Optional[ChartSpec] = None
    provider:             str                 = "gemini"
    model_used:           Optional[str]       = None
    certified_report_id:  Optional[str]       = None
    similarity_score:     Optional[float]     = None
    error:                Optional[str]       = None
    session_id:           Optional[str]       = None
    workspace_id:         Optional[str]       = None
    dashboard:            Optional[dict]      = None   # Mini-dashboard for chat questions


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    index_path = os.path.join(ui_dir, "index.html")
    if not os.path.exists(index_path):
        return JSONResponse({"message": "GenBI API running. Visit /docs for the API explorer."})
    return FileResponse(index_path)


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    """
    Main endpoint. Routes to a workspace-specific Orchestrator when
    workspace_id is provided, otherwise uses the default Orchestrator.
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    valid_providers = ["gemini", "openrouter", "ollama", "ag_chat"]
    if req.provider not in valid_providers:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{req.provider}'. Valid: {valid_providers}",
        )

    if req.provider == "ag_chat":
        if not req.workspace_id:
            raise HTTPException(
                status_code=400,
                detail="workspace_id is required for ag_chat provider.",
            )

        from api.workspace_routes import _get_manager
        try:
            mgr = _get_manager(req.workspace_id)
            workspace_dir = mgr.workspace_dir
        except Exception as e:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace not found: {e}",
            )

        req_file = os.path.join(workspace_dir, "query_request.json")
        res_file = os.path.join(workspace_dir, "query_response.json")

        for fpath in [req_file, res_file]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

        with open(req_file, "w") as f:
            json.dump({
                "workspace_id": req.workspace_id,
                "session_id": req.session_id,
                "question": req.question,
                "model": req.model,
                "provider": "ag_chat",
                "status": "pending"
            }, f)

        timeout_seconds = 200
        completed = False
        response_data = None

        for _ in range(timeout_seconds):
            await asyncio.sleep(1)
            if os.path.exists(res_file):
                try:
                    with open(res_file) as f:
                        response_data = json.load(f)
                    if response_data.get("status") == "completed":
                        completed = True
                        break
                    elif response_data.get("status") == "failed":
                        raise HTTPException(
                            status_code=500,
                            detail=f"AG Chat query execution failed: {response_data.get('error')}"
                        )
                except (OSError, json.JSONDecodeError):
                    pass

        for fpath in [req_file, res_file]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

        if not completed:
            raise HTTPException(
                status_code=504,
                detail="AG Chat query execution timed out after 120s."
            )

        result = response_data.get("result", {})
        result.setdefault("provider", "ag_chat")
        result.setdefault("model_used", req.model)
        result.setdefault("workspace_id", req.workspace_id)

        # Generate a 2-4 panel mini-dashboard for the question (non-blocking best-effort)
        if result.get("source") != "error" and result.get("rows_data"):
            try:
                orchestrator = get_workspace_orchestrator(req.workspace_id)
                dash_spec = orchestrator.handle_dashboard(
                    mode="question",
                    question=req.question,
                    primary_result=result,
                    provider="openrouter",  # fallback provider for generating supplementary queries
                    model="openai/gpt-4o-mini",
                    session_id=req.session_id,
                )
                dash_dict = dash_spec.model_dump()
                # Persist the question dashboard
                import hashlib
                q_key = "q_" + hashlib.md5(req.question.encode()).hexdigest()[:12]
                _save_dashboard(dash_dict, req.workspace_id, q_key)
                result["dashboard"] = dash_dict
            except Exception as _de:
                print(f"[/ask] Question dashboard generation failed for ag_chat (non-fatal): {_de}")

        return AskResponse(**result)

    if not req.workspace_id:
        raise HTTPException(
            status_code=400,
            detail="workspace_id is required."
        )

    orchestrator = get_workspace_orchestrator(req.workspace_id)

    result = orchestrator.handle(
        req.question,
        provider=req.provider,
        model=req.model,
        session_id=req.session_id,
    )

    result.setdefault("provider", req.provider)
    result.setdefault("model_used", req.model)
    if req.workspace_id:
        result["workspace_id"] = req.workspace_id

    # Generate a 2-4 panel mini-dashboard for the question (non-blocking best-effort)
    if result.get("source") != "error" and result.get("rows_data") and req.provider != "ag_chat":
        try:
            dash_spec = orchestrator.handle_dashboard(
                mode="question",
                question=req.question,
                primary_result=result,
                provider=req.provider,
                model=req.model,
                session_id=req.session_id,
            )
            dash_dict = dash_spec.model_dump()
            # Persist the question dashboard
            import hashlib
            q_key = "q_" + hashlib.md5(req.question.encode()).hexdigest()[:12]
            _save_dashboard(dash_dict, req.workspace_id, q_key)
            result["dashboard"] = dash_dict
        except Exception as _de:
            print(f"[/ask] Question dashboard generation failed (non-fatal): {_de}")

    return AskResponse(**result)


@app.get("/health")
def health(workspace_id: Optional[str] = None):
    db_ok = True
    if workspace_id:
        try:
            orchestrator = get_workspace_orchestrator(workspace_id)
            db_ok = orchestrator.test_db_connection()
        except Exception:
            db_ok = False
    return {
        "status":         "ok" if db_ok else "degraded",
        "db_connected":   db_ok,
        "history_length": 0,
    }


@app.get("/models")
def list_models():
    return {
        "providers": [
            {
                "id":          "gemini",
                "label":       "Google Gemini",
                "description": "Gemini 2.0 Flash (default)",
                "models":      ["gemini-2.0-flash"],
            },
            {
                "id":          "openrouter",
                "label":       "OpenRouter",
                "description": "Route to 100+ models via OpenRouter's OpenAI-compatible API",
                "models": [
                    "openai/gpt-4o",
                    "openai/gpt-4o-mini",
                    "anthropic/claude-3-5-sonnet",
                    "anthropic/claude-3-haiku",
                    "google/gemini-2.0-flash-001",
                    "meta-llama/llama-3.1-70b-instruct",
                    "mistralai/mistral-large",
                    "qwen/qwen-2.5-72b-instruct",
                ],
            },
            {
                "id":          "ollama",
                "label":       "Ollama (local)",
                "description": "Run a model locally via Ollama",
                "models": [
                    "llama3.1:8b",
                    "qwen2.5:7b",
                    "qwen2.5-coder:7b",
                    "mistral:7b",
                ],
            },
        ]
    }


@app.get("/ollama/models")
def ollama_models():
    """
    Returns the list of locally installed Ollama models by hitting the
    Ollama REST API on localhost:11434.

    FIX #17: urllib.request and json imported at module level (not inside
    the handler on every request).
    """
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        req  = urllib.request.Request(f"{ollama_url}/api/tags")
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        models = [m["name"] for m in data.get("models", [])]
        return {"models": models, "ollama_url": ollama_url}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not reach Ollama at {ollama_url}. "
                f"Make sure 'ollama serve' is running. Error: {e}"
            ),
        )


@app.get("/history")
def history(session_id: Optional[str] = None, workspace_id: Optional[str] = None):
    if workspace_id:
        try:
            orchestrator = get_workspace_orchestrator(workspace_id)
            return {"history": orchestrator.get_history(session_id=session_id)}
        except Exception:
            return {"history": []}
    return {"history": []}


@app.delete("/history")
def clear_history(session_id: Optional[str] = None, workspace_id: Optional[str] = None):
    if workspace_id:
        try:
            orchestrator = get_workspace_orchestrator(workspace_id)
            orchestrator.clear_history(session_id=session_id)
        except Exception:
            pass
    return {"status": "cleared"}


# ── Dashboard endpoints ───────────────────────────────────────────────────────

import uuid as _uuid
from fastapi.responses import StreamingResponse
import io

class DashboardGenerateRequest(BaseModel):
    workspace_id: str
    strategy:     str           = "auto"   # "auto" | "llm"
    provider:     str           = "gemini"
    model:        Optional[str] = None


def _dashboard_dir(workspace_id: str) -> str:
    from api.workspace_routes import _get_manager
    mgr = _get_manager(workspace_id)
    d = os.path.join(mgr.workspace_dir, "dashboards")
    os.makedirs(d, exist_ok=True)
    return d


def _exports_dir(workspace_id: str) -> str:
    from api.workspace_routes import _get_manager
    mgr = _get_manager(workspace_id)
    d = os.path.join(mgr.workspace_dir, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def _save_dashboard(spec_dict: dict, workspace_id: str, key: str) -> None:
    """Persist a dashboard JSON to the workspace dashboards dir."""
    ddir = _dashboard_dir(workspace_id)
    path = os.path.join(ddir, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec_dict, f, indent=2, default=str)


def _load_dashboard(workspace_id: str, key: str) -> Optional[dict]:
    ddir = _dashboard_dir(workspace_id)
    path = os.path.join(ddir, f"{key}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.post("/dashboard/generate")
async def dashboard_generate(req: DashboardGenerateRequest):
    """
    Generate (or regenerate) the General Dashboard for a workspace.
    strategy: "auto" → schema-derived queries | "llm" → LLM proposes questions
    """
    if req.provider == "ag_chat":
        # Route through file polling for AG Chat IDE Agent
        from api.workspace_routes import _get_manager
        try:
            mgr = _get_manager(req.workspace_id)
            workspace_dir = mgr.workspace_dir
        except Exception as e:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace not found: {e}",
            )

        req_file = os.path.join(workspace_dir, "dashboard_request.json")
        res_file = os.path.join(workspace_dir, "dashboard_response.json")

        for fpath in [req_file, res_file]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

        with open(req_file, "w") as f:
            json.dump({
                "workspace_id": req.workspace_id,
                "strategy": req.strategy,
                "provider": "ag_chat",
                "model": req.model,
                "status": "pending"
            }, f)

        timeout_seconds = 200
        completed = False
        response_data = None

        for _ in range(timeout_seconds):
            await asyncio.sleep(1)
            if os.path.exists(res_file):
                try:
                    with open(res_file) as f:
                        response_data = json.load(f)
                    if response_data.get("status") == "completed":
                        completed = True
                        break
                    elif response_data.get("status") == "failed":
                        raise HTTPException(
                            status_code=500,
                            detail=f"AG Chat dashboard generation failed: {response_data.get('error')}"
                        )
                except (OSError, json.JSONDecodeError):
                    pass

        for fpath in [req_file, res_file]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

        if not completed:
            raise HTTPException(
                status_code=504,
                detail="AG Chat dashboard generation timed out after 200s."
            )

        result = response_data.get("result", {})
        # Save dashboard spec back so list/get works
        key = f"general_{req.strategy}"
        _save_dashboard(result, req.workspace_id, key)
        return result

    # Non-ag_chat: run locally on backend
    try:
        orchestrator = get_workspace_orchestrator(req.workspace_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {e}")

    try:
        spec = orchestrator.handle_dashboard(
            mode="general",
            strategy=req.strategy,
            provider=req.provider,
            model=req.model,
        )
        spec_dict = spec.model_dump()
        key = f"general_{req.strategy}"
        _save_dashboard(spec_dict, req.workspace_id, key)
        return spec_dict
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Dashboard generation failed: {e}")


@app.get("/dashboard/list")
def dashboard_list(workspace_id: str):
    """List all saved dashboards for a workspace."""
    try:
        ddir = _dashboard_dir(workspace_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {e}")

    dashboards = []
    for fname in os.listdir(ddir):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(ddir, fname), encoding="utf-8") as f:
                    d = json.load(f)
                dashboards.append({
                    "id": d.get("id"),
                    "title": d.get("title"),
                    "mode": d.get("mode"),
                    "strategy": d.get("strategy"),
                    "question": d.get("question"),
                    "panel_count": len(d.get("panels", [])),
                    "created_at": d.get("created_at"),
                    "key": fname.replace(".json", ""),
                })
            except Exception:
                pass
    return {"dashboards": dashboards}


@app.get("/dashboard/{dashboard_id}")
def dashboard_get(workspace_id: str, dashboard_id: str):
    """Fetch a saved DashboardSpec by its ID or key."""
    # Try by key first (e.g. "general_auto"), then scan by id field
    spec = _load_dashboard(workspace_id, dashboard_id)
    if spec:
        return spec

    # Scan all dashboards for matching id
    try:
        ddir = _dashboard_dir(workspace_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {e}")

    for fname in os.listdir(ddir):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(ddir, fname), encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("id") == dashboard_id:
                    return d
            except Exception:
                pass

    raise HTTPException(status_code=404, detail=f"Dashboard '{dashboard_id}' not found.")


@app.get("/dashboard/data/{dashboard_id}")
def dashboard_data(workspace_id: str, dashboard_id: str):
    """
    Live data endpoint — serves all panel rows as JSON.
    Used by the .pbix live-data template M query.
    """
    # Search by spec id
    try:
        ddir = _dashboard_dir(workspace_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {e}")

    for fname in os.listdir(ddir):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(ddir, fname), encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("id") == dashboard_id:
                    # Return all panels' rows keyed by panel title
                    return {
                        "dashboard_id": dashboard_id,
                        "title": d.get("title"),
                        "panels": [
                            {
                                "id": p.get("id"),
                                "title": p.get("title"),
                                "rows": p.get("rows_data", []),
                                "chart_spec": p.get("chart_spec", {}),
                            }
                            for p in d.get("panels", [])
                        ],
                    }
            except Exception:
                pass

    raise HTTPException(status_code=404, detail=f"Dashboard data for '{dashboard_id}' not found.")


@app.get("/dashboard/export/{dashboard_id}")
async def dashboard_export(
    workspace_id: str,
    dashboard_id: str,
    format: str = "pbip",
    provider: Optional[str] = None,
    model: Optional[str] = None,
):
    """
    Export a dashboard as a Power BI file.
    format: "pbip" | "pbix" | "pbit"
    """
    from agents.powerbi_exporter import PowerBIExporter
    from agents.dashboard_agent import DashboardSpec

    # Load the spec
    spec_dict = None
    try:
        ddir = _dashboard_dir(workspace_id)
        for fname in os.listdir(ddir):
            if fname.endswith(".json"):
                with open(os.path.join(ddir, fname), encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("id") == dashboard_id or fname == f"{dashboard_id}.json":
                    spec_dict = d
                    break
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {e}")

    if not spec_dict:
        raise HTTPException(status_code=404, detail=f"Dashboard '{dashboard_id}' not found.")

    try:
        spec = DashboardSpec(**spec_dict)
        # Determine API base URL from request (use localhost as default)
        api_base = "http://localhost:8000"
        
        resolved_provider = provider if provider else os.environ.get("DEFAULT_PROVIDER", "gemini")
        resolved_model = model if model else os.environ.get("DEFAULT_MODEL", "gemini-2.0-flash")

        design_plan = None
        if resolved_provider == "ag_chat":
            from api.workspace_routes import _get_workspace_dir
            workspace_dir = _get_workspace_dir(workspace_id)
            
            req_file = os.path.join(workspace_dir, "export_request.json")
            res_file = os.path.join(workspace_dir, "export_response.json")

            for fpath in [req_file, res_file]:
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass

            panels_list = []
            for p in spec.panels:
                panels_list.append({
                    "id": p.id,
                    "title": p.title,
                    "chart_type": p.chart_spec.get("chart_type", "table") if p.chart_spec else "table",
                    "columns": list(p.rows_data[0].keys()) if p.rows_data else [],
                    "rows_count": len(p.rows_data)
                })

            with open(req_file, "w", encoding="utf-8") as f:
                json.dump({
                    "workspace_id": workspace_id,
                    "dashboard_id": dashboard_id,
                    "format": format,
                    "title": spec.title,
                    "panels": panels_list,
                    "provider": resolved_provider,
                    "model": resolved_model,
                    "status": "pending"
                }, f, indent=2)

            timeout_seconds = 200
            completed = False
            response_data = None

            for _ in range(timeout_seconds):
                await asyncio.sleep(1)
                if os.path.exists(res_file):
                    try:
                        with open(res_file, encoding="utf-8") as f:
                            response_data = json.load(f)
                        if response_data.get("status") == "completed":
                            completed = True
                            break
                        elif response_data.get("status") == "failed":
                            raise HTTPException(
                                status_code=500,
                                detail=f"AG Chat export design failed: {response_data.get('error')}"
                            )
                    except (OSError, json.JSONDecodeError):
                        pass

            for fpath in [req_file, res_file]:
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass

            if not completed:
                raise HTTPException(
                    status_code=504,
                    detail="AG Chat export design timed out after 200s."
                )
            
            design_plan = response_data.get("result")
        elif resolved_provider == "openrouter" and (not resolved_model or "gemini" in resolved_model):
            resolved_model = "openai/gpt-4o-mini"

        exporter = PowerBIExporter(
            spec,
            api_base_url=api_base,
            provider=resolved_provider,
            model=resolved_model,
            design_plan=design_plan
        )
        data, filename, content_type = exporter.export(format=format)
    except Exception as e:
        import traceback; traceback.print_exc()
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    return StreamingResponse(
        io.BytesIO(data),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="127.0.0.1", port=8002, reload=True)