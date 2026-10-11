"""
watch_workspace.py
-------------------
Background watcher process launched by api/main.py at startup.

Polls every workspace directory under WORKSPACE_ROOT for a
``generate_request.json`` file written by wait_for_ag_chat().

For provider == "ag_chat":
    This watcher does NOTHING. Antigravity 2.0 Chat monitors the workspace
    directories itself, performs schema inference / glossary generation using
    its own LLM + project file access, writes the output files
    (catalog.yaml / glossary_draft.json), and sets status="completed" in
    generate_request.json. The FastAPI wait_for_ag_chat() coroutine then
    picks up the completed status and returns the result to the UI.

For provider == "gemini" | "openrouter" | "ollama":
    This watcher calls the Python LLM pipeline directly and writes the
    output files itself, then marks status="completed".

Lifecycle
---------
- The main FastAPI process spawns this script via subprocess.Popen.
- Runs in an infinite loop, sleeping POLL_INTERVAL seconds between scans.
- Gracefully handles KeyboardInterrupt / SIGTERM from the parent process.

AG Chat contract (what AG Chat must do when it picks up a request):
--------------------------------------------------------------------
Read:  workspaces/<id>/generate_request.json
         { "workspace_id": "...", "task": "infer_schema"|"generate_glossary",
           "provider": "ag_chat", "status": "pending" }
       workspaces/<id>/meta.json          ← table/column metadata
       workspaces/<id>/catalog.yaml       ← (for glossary task) existing catalog

Write: workspaces/<id>/catalog.yaml      ← (infer_schema task)
       workspaces/<id>/certified_reports.yaml  ← (infer_schema task, empty list)
       workspaces/<id>/glossary_draft.json     ← (generate_glossary task)
         [ { "term": "...", "definition": "...", "maps_to": "..." }, ... ]

Then update generate_request.json → status="completed"  (or "failed" + "error")
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

# ── Ensure project root is on sys.path ───────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import yaml

from workspace.schema_inferrer    import infer_schema, save_catalog, save_certified_reports
from workspace.glossary_generator import generate_glossary

# ── Configuration ─────────────────────────────────────────────────────────────

POLL_INTERVAL    = 2          # seconds between workspace scans
REQUEST_FILENAME = "generate_request.json"
QUERY_FILENAME   = "query_request.json"
RESPONSE_FILENAME = "query_response.json"
DRAFT_FILENAME   = "glossary_draft.json"


def _workspace_root() -> str:
    return os.getenv(
        "WORKSPACE_ROOT",
        os.path.join(_HERE, "workspaces"),
    )


# ── File helpers ──────────────────────────────────────────────────────────────

def _load_request(req_file: str) -> dict | None:
    try:
        with open(req_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[watcher] Could not read {req_file}: {e}")
        return None


def _mark_completed(req_file: str) -> None:
    try:
        with open(req_file) as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["status"] = "completed"
    with open(req_file, "w") as f:
        json.dump(data, f)


def _mark_failed(req_file: str, error: str) -> None:
    try:
        with open(req_file) as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["status"] = "failed"
    data["error"]  = error
    with open(req_file, "w") as f:
        json.dump(data, f)


def _load_meta(workspace_dir: str) -> dict:
    meta_file = os.path.join(workspace_dir, "meta.json")
    if os.path.exists(meta_file):
        with open(meta_file) as f:
            return json.load(f)
    return {}


def _load_catalog(workspace_dir: str) -> dict:
    catalog_path = os.path.join(workspace_dir, "catalog.yaml")
    if os.path.exists(catalog_path):
        with open(catalog_path) as f:
            return yaml.safe_load(f) or {}
    meta   = _load_meta(workspace_dir)
    tables = meta.get("tables", {})
    return {
        "tables": {
            tname: {
                "description": "",
                "columns": {col: "" for col in info.get("columns", {}).keys()},
                "joins": [],
            }
            for tname, info in tables.items()
        },
        "glossary": {},
    }


# ── Task handlers (non-ag_chat providers only) ────────────────────────────────

def handle_infer_schema(workspace_dir: str, provider: str, model: str | None) -> None:
    meta   = _load_meta(workspace_dir)
    tables = meta.get("tables", {})

    if not tables:
        meta_file = os.path.join(workspace_dir, "meta.json")
        if not os.path.exists(meta_file):
            raise ValueError(
                "meta.json not found — workspace not initialised properly."
            )
        raise ValueError(
            "No tables in meta.json. "
            "For SQLite: verify the file path points to an existing DB with tables. "
            "For other databases: check schema name and user permissions, then "
            "re-run the DB connection step."
        )

    print(f"[watcher] infer_schema: {list(tables.keys())} via {provider}")
    catalog = infer_schema(tables, provider=provider, model=model)

    catalog_path = os.path.join(workspace_dir, "catalog.yaml")
    reports_path = os.path.join(workspace_dir, "certified_reports.yaml")
    save_catalog(catalog, catalog_path)
    save_certified_reports(reports_path)

    meta["status"] = "schema_ready"
    with open(os.path.join(workspace_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)

    print(f"[watcher] infer_schema done → {catalog_path}")


def handle_generate_glossary(workspace_dir: str, provider: str, model: str | None) -> None:
    catalog = _load_catalog(workspace_dir)
    if not catalog.get("tables"):
        raise ValueError("Catalog has no tables — run schema inference first.")

    print(f"[watcher] generate_glossary via {provider}")
    glossary   = generate_glossary(catalog, provider=provider, model=model)
    draft_file = os.path.join(workspace_dir, DRAFT_FILENAME)
    with open(draft_file, "w") as f:
        json.dump(glossary, f, indent=2, ensure_ascii=False)

    print(f"[watcher] generate_glossary done → {draft_file} ({len(glossary)} terms)")


# ── AG Chat query handler ─────────────────────────────────────────────────────

def handle_query_request(workspace_dir: str, query_file: str, res_file: str) -> None:
    """Pick up a pending query_request.json, run the orchestrator, write query_response.json."""
    data = _load_request(query_file)
    if data is None or data.get("status") != "pending":
        return

    provider = data.get("provider", "ag_chat")
    if provider == "ag_chat":
        # Leave it for Antigravity 2.0 Chat (the IDE Agent) to process
        return

    workspace_id = data.get("workspace_id", os.path.basename(workspace_dir))
    question     = data.get("question", "")
    session_id   = data.get("session_id")
    model        = data.get("model") or ""

    print(f"[watcher] query: '{question}' for workspace '{workspace_id}' via {provider}")

    try:
        # Use the same orchestrator pipeline as /ask
        from api.main import get_workspace_orchestrator
        orchestrator = get_workspace_orchestrator(workspace_id)
        result = orchestrator.handle(
            question,
            provider=provider,
            model=model or None,
            session_id=session_id,
        )
        result.setdefault("provider", provider)
        result.setdefault("model_used", model)
        result["workspace_id"] = workspace_id

        with open(res_file, "w") as f:
            json.dump({"status": "completed", "result": result}, f, default=str)
        print(f"[watcher] ag_chat query completed for workspace '{workspace_id}'")

    except Exception as exc:
        traceback.print_exc()
        print(f"[watcher] ag_chat query FAILED for workspace '{workspace_id}': {exc}")
        with open(res_file, "w") as f:
            json.dump({"status": "failed", "error": str(exc)}, f)


# ── Core poll loop ─────────────────────────────────────────────────────────────

def process_request(workspace_dir: str, req_file: str) -> None:
    data = _load_request(req_file)
    if data is None:
        return

    if data.get("status") != "pending":
        return  # completed/failed already handled (by us or by AG Chat)

    task     = data.get("task", "")
    provider = data.get("provider", "gemini")
    wid      = data.get("workspace_id", os.path.basename(workspace_dir))

    # ── AG Chat requests: leave the file alone — AG Chat handles it ──────────
    if provider == "ag_chat":
        # Do not touch the file. Antigravity 2.0 Chat monitors workspace dirs,
        # sees the pending request, performs the task using its own LLM +
        # project context, writes output files, and sets status="completed".
        # wait_for_ag_chat() in workspace_routes.py will detect that change.
        return

    # ── Non-ag_chat providers: run the Python LLM pipeline directly ──────────
    model = data.get("model")  # may be None → provider default

    print(f"[watcher] '{task}' for workspace '{wid}' via {provider}")

    try:
        if task == "infer_schema":
            handle_infer_schema(workspace_dir, provider, model)
        elif task == "generate_glossary":
            handle_generate_glossary(workspace_dir, provider, model)
        else:
            raise ValueError(f"Unknown task '{task}'")

        _mark_completed(req_file)
        print(f"[watcher] '{task}' completed for workspace '{wid}'")

    except Exception as exc:
        traceback.print_exc()
        print(f"[watcher] '{task}' FAILED for workspace '{wid}': {exc}")
        _mark_failed(req_file, str(exc))


def scan_workspaces() -> None:
    root = _workspace_root()
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        wdir     = os.path.join(root, name)
        if not os.path.isdir(wdir):
            continue

        # Handle generate_request.json (schema / glossary tasks)
        req_file = os.path.join(wdir, REQUEST_FILENAME)
        if os.path.exists(req_file):
            try:
                process_request(wdir, req_file)
            except Exception as e:
                print(f"[watcher] Unexpected error for workspace '{name}': {e}")

        # Handle query_request.json (ag_chat chat queries)
        query_file = os.path.join(wdir, QUERY_FILENAME)
        res_file   = os.path.join(wdir, RESPONSE_FILENAME)
        if os.path.exists(query_file):
            try:
                handle_query_request(wdir, query_file, res_file)
            except Exception as e:
                print(f"[watcher] Unexpected query error for workspace '{name}': {e}")


def main() -> None:
    print(f"[watcher] Started. Polling every {POLL_INTERVAL}s. "
          f"AG Chat requests are passed through to Antigravity 2.0.")
    try:
        while True:
            scan_workspaces()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("[watcher] Shutting down.")


if __name__ == "__main__":
    main()
