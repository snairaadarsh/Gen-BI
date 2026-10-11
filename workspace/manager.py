"""
workspace/manager.py
----------------------
WorkspaceManager owns the lifecycle of a per-workspace data environment:
  - Creates and tracks the workspace directory under WORKSPACE_ROOT
  - Delegates CSV ingestion / DB connection to the appropriate module
  - Delegates schema inference and glossary generation to LLM helpers
  - Writes workspace-specific catalog.yaml + certified_reports.yaml
  - Rebuilds FAISS indexes when the catalog changes
  - Produces a ready-to-use Orchestrator pointing at the workspace data

One WorkspaceManager instance per workspace_id. The API layer (main.py)
holds a dict of active managers keyed by workspace_id.

FIX #15: catalog dict is no longer stored in meta.json. The catalog lives
         exclusively in catalog.yaml. meta.json is now a lightweight status
         file (workspace_id, status, source_type, table column names only).
         This prevents meta.json from bloating to megabytes for wide schemas.

FIX #24: _workspace_root() is called at call time everywhere instead of
         using the module-level WORKSPACE_ROOT alias, so env-var changes
         after import are respected by all methods.

FIX #16: Removed sys.path.insert() calls from workspace helper modules.
         The project root is on sys.path via api/main.py; normal imports work.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional

import yaml

from workspace.csv_ingestor    import ingest_csvs, get_table_summary
from workspace.db_connector    import (
    build_connection_string, test_connection as _test_conn,
    reflect_schema, save_connection, load_connection,
)
from workspace.schema_inferrer import infer_schema, save_catalog, save_certified_reports
from workspace.glossary_generator import (
    generate_glossary, merge_glossary_into_catalog,
)

_HERE         = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _workspace_root() -> str:
    """Resolve WORKSPACE_ROOT at call time so .env changes after import take effect."""
    return os.getenv("WORKSPACE_ROOT", os.path.join(_PROJECT_ROOT, "workspaces"))


# Module-level alias kept for import compatibility in workspace_routes.py,
# but NOTE: prefer calling _workspace_root() directly in any new code.
WORKSPACE_ROOT = _workspace_root()


# ── Status constants ────────────────────────────────────────────────────────────

STATUS_CREATED   = "created"
STATUS_INGESTED  = "ingested"
STATUS_SCHEMA    = "schema_ready"
STATUS_GLOSSARY  = "glossary_ready"
STATUS_READY     = "ready"


class WorkspaceManager:
    def __init__(self, workspace_id: str):
        self.workspace_id  = workspace_id
        # FIX #24: resolve workspace_dir at construction time via _workspace_root()
        self.workspace_dir = os.path.join(_workspace_root(), workspace_id)
        os.makedirs(self.workspace_dir, exist_ok=True)

        self._meta_file = os.path.join(self.workspace_dir, "meta.json")
        self._meta      = self._load_meta()

        self._orchestrator = None

    # ── Factory ────────────────────────────────────────────────────────────────

    @classmethod
    def create_new(cls) -> "WorkspaceManager":
        wid = str(uuid.uuid4())
        mgr = cls(wid)
        mgr._meta["status"] = STATUS_CREATED
        mgr._save_meta()
        return mgr

    @classmethod
    def load(cls, workspace_id: str) -> Optional["WorkspaceManager"]:
        """Return an existing WorkspaceManager or None if not found."""
        wdir = os.path.join(_workspace_root(), workspace_id)
        if not os.path.isdir(wdir):
            return None
        return cls(workspace_id)

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._meta.get("status", STATUS_CREATED)

    @property
    def db_path(self) -> str:
        return os.path.join(self.workspace_dir, "data.db")

    @property
    def catalog_path(self) -> str:
        return os.path.join(self.workspace_dir, "catalog.yaml")

    @property
    def reports_path(self) -> str:
        return os.path.join(self.workspace_dir, "certified_reports.yaml")

    @property
    def faiss_catalog_dir(self) -> str:
        return os.path.join(self.workspace_dir, "faiss_index", "catalog")

    @property
    def faiss_reports_dir(self) -> str:
        return os.path.join(self.workspace_dir, "faiss_index", "certified_reports")

    def get_db_url(self) -> str:
        """Return the SQLAlchemy URL for this workspace's data source."""
        conn_str = load_connection(self.workspace_dir)
        if conn_str:
            return conn_str
        return f"sqlite:///{self.db_path}"

    # ── Phase 1: Data ingestion ─────────────────────────────────────────────────

    def ingest_csv_files(self, file_paths: list[str]) -> dict:
        """Load CSV files into this workspace's SQLite DB."""
        summary = ingest_csvs(file_paths, self.db_path)
        # FIX #15: store only lightweight table metadata (column names + row counts),
        # not the full catalog dict.
        self._meta["tables"]      = {
            tname: {
                "source_file": info["source_file"],
                "row_count":   info["row_count"],
                "columns":     info["columns"],
            }
            for tname, info in summary["tables"].items()
        }
        self._meta["source_type"] = "csv"
        self._meta["status"]      = STATUS_INGESTED
        self._save_meta()
        return summary

    def connect_external_db(
        self,
        db_type: str,
        host: str = "",
        port: str = "",
        database: str = "",
        username: str = "",
        password: str = "",
    ) -> tuple[bool, str]:
        """Test and persist an external DB connection."""
        conn_str = build_connection_string(db_type, host, port, database, username, password)
        ok, msg  = _test_conn(conn_str)
        if ok:
            save_connection(self.workspace_dir, conn_str)
            schema = reflect_schema(conn_str)  # raises ValueError if no tables found
            if not schema.get("tables"):
                return False, (
                    "Connection succeeded but no tables were found. "
                    "Check your database path/schema and permissions."
                )
            self._meta["tables"]      = schema["tables"]
            self._meta["source_type"] = "external_db"
            self._meta["status"]      = STATUS_INGESTED
            self._save_meta()
        return ok, msg

    # ── Phase 2: Schema inference ───────────────────────────────────────────────

    def infer_schema(self, provider: str = "gemini", model: str | None = None) -> dict:
        """
        Call LLM to infer catalog from the ingested table summary.
        Saves catalog.yaml and returns the catalog dict for frontend display.

        FIX #15: No longer stores the full catalog in meta.json.
        """
        tables  = self._meta.get("tables", {})
        catalog = infer_schema(tables, provider=provider, model=model)

        save_catalog(catalog, self.catalog_path)
        save_certified_reports(self.reports_path)

        # FIX #15: Do NOT store the full catalog in meta.json.
        self._meta["status"] = STATUS_SCHEMA
        self._save_meta()
        return catalog

    def save_schema(self, catalog: dict) -> None:
        """Save a user-edited catalog dict (from the frontend schema editor)."""
        save_catalog(catalog, self.catalog_path)
        # FIX #15: Do NOT mirror the catalog into meta.json.
        self._meta["status"] = STATUS_SCHEMA
        self._save_meta()

    # ── Phase 3: Glossary ───────────────────────────────────────────────────────

    def generate_glossary(self, provider: str = "gemini", model: str | None = None) -> list[dict]:
        """Generate a glossary draft from the current catalog."""
        catalog = self._load_catalog()
        return generate_glossary(catalog, provider=provider, model=model)

    def save_glossary(self, glossary_rows: list[dict]) -> None:
        """
        Merge reviewed glossary into catalog.yaml and rebuild FAISS indexes.
        Sets status to READY.
        """
        catalog = self._load_catalog()
        catalog = merge_glossary_into_catalog(catalog, glossary_rows)
        save_catalog(catalog, self.catalog_path)

        # FIX #15: Do NOT store the full catalog in meta.json.
        self._meta["status"] = STATUS_GLOSSARY
        self._save_meta()

        self._rebuild_indexes()

        self._meta["status"] = STATUS_READY
        self._save_meta()

    # ── Phase 4: Orchestrator ───────────────────────────────────────────────────

    def get_orchestrator(self):
        """
        Return (or build) an Orchestrator instance pointing at this workspace.
        Only callable once status == STATUS_READY.
        """
        if self._orchestrator is None:
            from agents.orchestrator import Orchestrator
            self._orchestrator = Orchestrator(
                db_url=self.get_db_url(),
                catalog_path=self.catalog_path,
                catalog_index_dir=self.faiss_catalog_dir,
                reports_path=self.reports_path,
                reports_index_dir=self.faiss_reports_dir,
                workspace_id=self.workspace_id,
            )
        return self._orchestrator

    def invalidate_orchestrator(self):
        """Force the next get_orchestrator() call to rebuild."""
        self._orchestrator = None

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _load_meta(self) -> dict:
        if os.path.exists(self._meta_file):
            with open(self._meta_file) as f:
                return json.load(f)
        return {}

    def _save_meta(self):
        with open(self._meta_file, "w") as f:
            json.dump(self._meta, f, indent=2, default=str)

    def get_baseline_catalog(self) -> dict:
        """Construct a baseline catalog from the reflected tables in _meta."""
        tables = self._meta.get("tables", {})
        catalog: dict = {"tables": {}, "glossary": {}}
        for tname, tinfo in tables.items():
            cols = tinfo.get("columns", {})
            catalog["tables"][tname] = {
                "description": "",
                "columns": {col: "" for col in cols.keys()},
                "joins": [],
            }
        return catalog

    def _load_catalog(self) -> dict:
        """
        Load catalog from catalog.yaml (authoritative source).
        Falls back to a baseline catalog derived from meta.json table list
        if catalog.yaml does not yet exist.

        FIX #15: No longer falls back to meta.json['catalog'] — the catalog
        is never stored there.
        """
        if os.path.exists(self.catalog_path):
            with open(self.catalog_path) as f:
                return yaml.safe_load(f) or {}
        return self.get_baseline_catalog()

    def _rebuild_indexes(self):
        """Force MetadataAgent and RagAgent to rebuild their FAISS indexes."""
        from agents.metadata_agent import MetadataAgent
        from agents.rag_agent      import RagAgent

        print(f"[WorkspaceManager] Rebuilding FAISS indexes for workspace {self.workspace_id}")

        meta = MetadataAgent(
            catalog_path=self.catalog_path,
            index_dir=self.faiss_catalog_dir,
        )
        meta.rebuild_index()

        rag = RagAgent(
            reports_path=self.reports_path,
            index_dir=self.faiss_reports_dir,
        )
        rag.rebuild_index()

        self.invalidate_orchestrator()
        print(f"[WorkspaceManager] FAISS indexes rebuilt for workspace {self.workspace_id}")

    def to_status_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "status":       self.status,
            "source_type":  self._meta.get("source_type"),
            "table_names":  list(self._meta.get("tables", {}).keys()),
        }