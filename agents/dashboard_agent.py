"""
agents/dashboard_agent.py
--------------------------
Generates a multi-panel DashboardSpec from a workspace.

Two modes:
  - "general": Builds a comprehensive executive dashboard (12-15 panels) for the entire dataset.
      strategy="auto"  derives queries from the schema automatically.
      strategy="llm"   asks the LLM to propose the best analytical questions.
  - "question": Generates 2-4 panels for a specific chat question
      (primary result + 1-3 supplementary context panels).

Each panel is produced by the existing SqlAgent -> ExecuteAgent -> BiAgent pipeline.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


# Data models

class DashboardPanel(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    chart_spec: dict
    rows_data: list[dict]
    narrative: str
    sql_used: Optional[str] = None


class DashboardSpec(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workspace_id: str
    title: str
    mode: Literal["general", "question"]
    strategy: Optional[Literal["auto", "llm"]] = None
    question: Optional[str] = None
    panels: list[DashboardPanel]
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


_LLM_PROPOSE_PROMPT = """You are a senior business intelligence analyst. Given the database schema below, propose exactly {n} analytical questions that would give an executive a COMPLETE overview of the business.

Requirements for the questions:
- Each question must be directly answerable from the schema alone using SQL.
- Cover a broad range: KPI totals, monthly/yearly time trends, top-N product/seller rankings, geographic breakdowns, payment method distributions, customer review analysis, delivery performance.
- Be very specific so a SQL query can be written immediately for each question.
- Include the word "by month" for time-series questions so the SQL agent writes the right GROUP BY.
- Write one question per line. Do NOT number them or add bullet points.

Schema:
{schema_summary}

Output exactly {n} questions, one per line:"""

_SUPPLEMENTARY_PROMPT = """You are a senior business analyst. A user asked: "{question}"

The primary answer has already been produced. Now propose {n} SHORT supplementary analytical questions that would add useful context or depth to this answer.

Requirements:
- Each question must be answerable from the schema below.
- Each must be different from the primary question.
- Do NOT repeat the primary question.
- One question per line, no numbering or bullet points.

Schema:
{schema_summary}

Output exactly {n} supplementary questions, one per line:"""


# Dashboard Agent

class DashboardAgent:
    """
    Orchestrates multi-panel dashboard generation.
    """

    def __init__(self, orchestrator, catalog: dict, workspace_id: str):
        self._orch = orchestrator
        self._catalog = catalog
        self._workspace_id = workspace_id

    def generate_general(
        self,
        strategy: str = "llm",
        provider: str = "gemini",
        model: str | None = None,
        session_id: str | None = None,
    ) -> DashboardSpec:
        """Generate a comprehensive executive overview dashboard (12 panels) using AI-proposed questions."""
        questions = self._llm_propose_questions(n=12, provider=provider, model=model)

        panels = self._run_questions(
            questions, provider=provider, model=model,
            session_id=session_id or f"dash-general-{uuid.uuid4().hex[:8]}"
        )

        return DashboardSpec(
            workspace_id=self._workspace_id,
            title="Executive Business Dashboard",
            mode="general",
            strategy="llm",
            panels=panels,
        )

    def generate_for_question(
        self,
        question: str,
        primary_result: dict,
        provider: str = "gemini",
        model: str | None = None,
        session_id: str | None = None,
    ) -> DashboardSpec:
        """Generate a 2-4 panel dashboard for a specific chat question."""
        primary_panel = self._result_to_panel(question, primary_result)
        supplementary_questions = self._llm_propose_supplementary(
            question=question, n=2, provider=provider, model=model
        )
        supp_panels = self._run_questions(
            supplementary_questions, provider=provider, model=model,
            session_id=session_id or f"dash-q-{uuid.uuid4().hex[:8]}"
        )
        all_panels = [primary_panel] + supp_panels
        return DashboardSpec(
            workspace_id=self._workspace_id,
            title=f"Dashboard: {question[:60]}{'...' if len(question) > 60 else ''}",
            mode="question",
            question=question,
            panels=all_panels,
        )

    def _schema_summary(self) -> str:
        lines = []
        tables = self._catalog.get("tables", {})
        for tname, tinfo in tables.items():
            desc = tinfo.get("description", "")
            cols = list((tinfo.get("columns") or {}).keys())
            col_str = ", ".join(cols[:15])
            if len(cols) > 15:
                col_str += f", ... (+{len(cols) - 15} more)"
            lines.append(f"Table: {tname}" + (f" - {desc}" if desc else ""))
            lines.append(f"  Columns: {col_str}")
        return "\n".join(lines) if lines else "No schema available."

    def _llm_propose_questions(self, n: int, provider: str, model: str | None) -> list[str]:
        from agents.llm_client import get_chat_model
        from langchain_core.messages import HumanMessage
        schema_summary = self._schema_summary()
        prompt = _LLM_PROPOSE_PROMPT.format(n=n, schema_summary=schema_summary)
        llm = get_chat_model(provider=provider, model=model, temperature=0.3)
        resp = llm.invoke([HumanMessage(content=prompt)])
        raw = (resp.content or "").strip()
        questions = [q.strip() for q in raw.splitlines() if q.strip()]
        if not questions:
            raise ValueError(f"[DashboardAgent] LLM returned no questions for dashboard proposal.")
        return questions[:n]

    def _llm_propose_supplementary(self, question: str, n: int, provider: str, model: str | None) -> list[str]:
        from agents.llm_client import get_chat_model
        from langchain_core.messages import HumanMessage
        schema_summary = self._schema_summary()
        prompt = _SUPPLEMENTARY_PROMPT.format(question=question, n=n, schema_summary=schema_summary)
        llm = get_chat_model(provider=provider, model=model, temperature=0.3)
        resp = llm.invoke([HumanMessage(content=prompt)])
        raw = (resp.content or "").strip()
        questions = [q.strip() for q in raw.splitlines() if q.strip()]
        return questions[:n]

    def _run_questions(self, questions: list[str], provider: str, model: str | None, session_id: str) -> list[DashboardPanel]:
        panels = []
        for i, q in enumerate(questions):
            try:
                result = self._orch.handle(
                    q, provider=provider, model=model,
                    session_id=f"{session_id}-panel-{i}",
                )
                if result.get("source") == "error":
                    print(f"[DashboardAgent] Panel '{q[:40]}' errored - skipping.")
                    continue
                if not result.get("rows_data"):
                    print(f"[DashboardAgent] Panel '{q[:40]}' returned no rows - skipping.")
                    continue
                panel = self._result_to_panel(q, result)
                panels.append(panel)
            except Exception as e:
                print(f"[DashboardAgent] Panel '{q[:40]}' exception: {e} - skipping.")
        return panels

    @staticmethod
    def _result_to_panel(question: str, result: dict) -> DashboardPanel:
        chart_spec = result.get("chart_spec") or {}
        title = (
            chart_spec.get("title")
            or question[:60] + ("..." if len(question) > 60 else "")
        )
        return DashboardPanel(
            title=title,
            chart_spec=chart_spec,
            rows_data=result.get("rows_data", []),
            narrative=result.get("answer", ""),
            sql_used=result.get("sql_used"),
        )
