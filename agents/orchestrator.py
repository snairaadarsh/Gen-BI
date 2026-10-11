"""
agents/orchestrator.py
-----------------------
Thin wrapper around the LangGraph-based GenBIGraph.

[UPGRADE] Constructor now accepts optional workspace config:
  - db_url (str | None): workspace-specific database URL
  - catalog_path (str | None): path to catalog.yaml
  - catalog_index_dir (str | None): FAISS index directory for catalog
  - reports_path (str | None): path to certified_reports.yaml
  - reports_index_dir (str | None): FAISS index directory for reports

All default to None → original behaviour (backward compatible).

FIX #6: Removed the duplicate self._history dict. Conversation history is
now read exclusively from the LangGraph MemorySaver state (turn_history),
which is the single source of truth used for multi-turn context. The
previous self._history was redundant and could drift from the graph state.
"""
from __future__ import annotations

import os
import uuid

import yaml

from agents.graph import GenBIGraph


class Orchestrator:
    def __init__(
        self,
        db_url: str | None = None,
        catalog_path: str | None = None,
        catalog_index_dir: str | None = None,
        reports_path: str | None = None,
        reports_index_dir: str | None = None,
        workspace_id: str | None = None,
    ):
        self._graph = GenBIGraph(
            db_url=db_url,
            catalog_path=catalog_path,
            catalog_index_dir=catalog_index_dir,
            reports_path=reports_path,
            reports_index_dir=reports_index_dir,
        )
        self._default_session_id = str(uuid.uuid4())
        self._catalog_path = catalog_path
        self._workspace_id = workspace_id or "default"

    def handle(
        self,
        question: str,
        provider: str = "gemini",
        model: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        sid = session_id or self._default_session_id

        final_state = self._graph.run(question, provider=provider, model=model, thread_id=sid)

        source = final_state.get("source", "error")
        sql    = final_state.get("sql")
        rows   = final_state.get("rows", [])
        answer = final_state.get("answer", "")
        error  = final_state.get("error")

        if error and not rows:
            return {
                "answer": answer,
                "source": "error",
                "sql_used": sql,
                "rows_returned": 0,
                "error": error,
                "session_id": sid,
            }

        response = {
            "answer": answer,
            "source": source,
            "sql_used": sql,
            "rows_returned": len(rows),
            "rows_data": rows,
            "chart_spec": final_state.get("chart_spec", {}),
            "session_id": sid,
        }

        certified = final_state.get("certified_match")
        if certified:
            response["certified_report_id"] = certified.get("id")
            response["similarity_score"]     = certified.get("similarity_score")

        return response

    def get_history(self, session_id: str | None = None) -> list[dict]:
        """
        Return turn history for the given session by reading from the
        LangGraph MemorySaver state — the single source of truth.
        Returns an empty list if the session has no state yet.
        """
        sid = session_id or self._default_session_id
        config = {"configurable": {"thread_id": sid}}
        try:
            state = self._graph._graph.get_state(config)
            if state and state.values:
                return list(state.values.get("turn_history", []))
        except Exception:
            pass
        return []

    def clear_history(self, session_id: str | None = None):
        sid = session_id or self._default_session_id
        self._graph.reset_thread(sid)

    def test_db_connection(self) -> bool:
        return self._graph.execute.test_connection()

    # ── Dashboard generation ────────────────────────────────────────────────────

    def handle_dashboard(
        self,
        mode: str = "general",
        strategy: str = "auto",
        question: str | None = None,
        primary_result: dict | None = None,
        provider: str = "gemini",
        model: str | None = None,
        session_id: str | None = None,
    ):
        """
        Generate a DashboardSpec.

        Args:
            mode:           "general" or "question"
            strategy:       "auto" or "llm" (general mode only)
            question:       The chat question (question mode only)
            primary_result: The existing orchestrator result for the primary question
            provider:       LLM provider string
            model:          Optional model override
            session_id:     Optional session ID prefix for panel isolation

        Returns:
            DashboardSpec instance
        """
        from agents.dashboard_agent import DashboardAgent

        catalog = self._load_catalog()
        agent = DashboardAgent(
            orchestrator=self,
            catalog=catalog,
            workspace_id=self._workspace_id,
        )

        if mode == "question":
            if not question or primary_result is None:
                raise ValueError("question and primary_result are required for question mode.")
            return agent.generate_for_question(
                question=question,
                primary_result=primary_result,
                provider=provider,
                model=model,
                session_id=session_id,
            )
        else:
            return agent.generate_general(
                strategy=strategy,
                provider=provider,
                model=model,
                session_id=session_id,
            )

    def _load_catalog(self) -> dict:
        """Load catalog.yaml if available, else return empty dict."""
        if self._catalog_path and os.path.exists(self._catalog_path):
            with open(self._catalog_path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}