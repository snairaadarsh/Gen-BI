"""
agents/graph.py
----------------
LangGraph StateGraph implementation of the GenBI agent pipeline.

[UPGRADE] GenBIGraph.__init__ now accepts optional workspace config:
  - db_url (str | None): if supplied, used for ExecuteAgent instead of env var
  - catalog_path (str | None): if supplied, used for MetadataAgent
  - catalog_index_dir (str | None): FAISS index dir for catalog
  - reports_path (str | None): if supplied, used for RagAgent
  - reports_index_dir (str | None): FAISS index dir for reports

All params default to None → original behaviour (backward compatible).

NOTE — MemorySaver limitation:
  MemorySaver stores conversation thread state in process RAM. This means:
  - State is lost on every server restart.
  - Running multiple uvicorn workers will give inconsistent memory across
    workers (different users may land on different workers).
  For production, replace MemorySaver with a persistent checkpointer such as
  langgraph.checkpoint.postgres.PostgresSaver or a Redis-backed equivalent.

FIX #18: _node_generate_sql now prints the full traceback on SQL gen failure
so that errors are diagnosable from server logs.
"""
from __future__ import annotations

import traceback
from typing import Optional, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from agents.metadata_agent import MetadataAgent
from agents.rag_agent import RagAgent
from agents.sql_agent import SqlAgent
from agents.execute_agent import ExecuteAgent
from agents.bi_agent import BiAgent

MAX_TURN_HISTORY = 5


class TurnRecord(TypedDict):
    question: str
    answer: str


class GenBIState(TypedDict, total=False):
    question: str
    provider: str
    model: Optional[str]
    turn_history: list[TurnRecord]
    context: str
    certified_match: Optional[dict]
    sql: Optional[str]
    source: str
    rows: list[dict]
    answer: str
    chart_spec: dict
    error: Optional[str]


class GenBIGraph:
    def __init__(
        self,
        db_url: str | None = None,
        catalog_path: str | None = None,
        catalog_index_dir: str | None = None,
        reports_path: str | None = None,
        reports_index_dir: str | None = None,
    ):
        metadata_kwargs = {}
        if catalog_path:
            metadata_kwargs["catalog_path"] = catalog_path
        if catalog_index_dir:
            metadata_kwargs["index_dir"] = catalog_index_dir

        rag_kwargs = {}
        if reports_path:
            rag_kwargs["reports_path"] = reports_path
        if reports_index_dir:
            rag_kwargs["index_dir"] = reports_index_dir

        # Determine dialect from db_url
        dialect = "sqlite"
        if db_url:
            db_type = db_url.split(":")[0].lower()
            if "postgresql" in db_type:
                dialect = "postgresql"
            elif "mysql" in db_type:
                dialect = "mysql"
            elif "mssql" in db_type:
                dialect = "mssql"
            elif "sqlite" in db_type:
                dialect = "sqlite"
            else:
                dialect = db_type

        self.metadata   = MetadataAgent(**metadata_kwargs)
        self.rag        = RagAgent(**rag_kwargs)
        self.sql_agent  = SqlAgent(dialect=dialect)
        self.execute    = ExecuteAgent(db_url=db_url)
        self.bi         = BiAgent()

        # NOTE: MemorySaver is in-process only. See module docstring for
        # production upgrade guidance.
        self._checkpointer = MemorySaver()
        self._graph = self._build_graph()

    # ── Graph construction ─────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(GenBIState)

        graph.add_node("metadata_context", self._node_metadata_context)
        graph.add_node("rag_lookup", self._node_rag_lookup)
        graph.add_node("generate_sql", self._node_generate_sql)
        graph.add_node("execute_sql", self._node_execute_sql)
        graph.add_node("bi_interpret", self._node_bi_interpret)

        graph.set_entry_point("metadata_context")
        graph.add_edge("metadata_context", "rag_lookup")

        graph.add_conditional_edges(
            "rag_lookup",
            self._route_after_rag,
            {"certified": "execute_sql", "generate": "generate_sql"},
        )

        graph.add_edge("generate_sql", "execute_sql")

        graph.add_conditional_edges(
            "execute_sql",
            self._route_after_execute,
            {"ok": "bi_interpret", "error": END},
        )

        graph.add_edge("bi_interpret", END)
        return graph.compile(checkpointer=self._checkpointer)

    # ── Nodes ──────────────────────────────────────────────────────────────────

    def _node_metadata_context(self, state: GenBIState) -> dict:
        recent = self._format_recent_turns(state.get("turn_history", []))
        question = state["question"]
        base_context = self.metadata.get_context(question)

        if recent:
            context = (
                f"{base_context}\n"
                f"--- Recent conversation (for resolving follow-up references "
                f"like 'that', 'those', 'last month' relative to prior turns) ---\n"
                f"{recent}\n"
            )
        else:
            context = base_context

        return {"context": context}

    def _node_rag_lookup(self, state: GenBIState) -> dict:
        certified = self.rag.find_match(state["question"])
        return {"certified_match": certified}

    def _route_after_rag(self, state: GenBIState) -> str:
        return "certified" if state.get("certified_match") else "generate"

    def _node_generate_sql(self, state: GenBIState) -> dict:
        # FIX #18: Print full traceback so SQL generation failures are
        # diagnosable from server logs rather than swallowed silently.
        try:
            sql = self.sql_agent.generate(
                state["question"],
                state["context"],
                provider=state.get("provider", "gemini"),
                model=state.get("model"),
            )
            return {"sql": sql, "source": "generated"}
        except Exception as e:
            print(f"[generate_sql] SQL generation failed: {e}")
            traceback.print_exc()
            return {
                "source": "error",
                "answer": f"I was unable to generate a SQL query for this question. Error: {e}",
                "error": str(e),
            }

    def _node_execute_sql(self, state: GenBIState) -> dict:
        if state.get("source") == "error":
            return {}

        certified = state.get("certified_match")
        sql = certified["sql"].strip() if certified else state["sql"]
        source = "certified_report" if certified else state.get("source", "generated")

        try:
            rows = self.execute.run(sql)
            return {"sql": sql, "source": source, "rows": rows}
        except Exception as e:
            if not certified:
                try:
                    healed_sql, rows = self.sql_agent.generate_with_self_heal(
                        state["question"],
                        state["context"],
                        execute_fn=self.execute.run,
                        provider=state.get("provider", "gemini"),
                        model=state.get("model"),
                    )
                    return {"sql": healed_sql, "source": source, "rows": rows}
                except Exception as e2:
                    print(f"[execute_sql] Self-heal exhausted: {e2}")
                    traceback.print_exc()
                    return {
                        "sql": sql, "source": source,
                        "answer": f"The query could not be executed. Error: {e2}",
                        "error": str(e2),
                    }
            print(f"[execute_sql] Certified report query failed: {e}")
            traceback.print_exc()
            return {
                "sql": sql, "source": source,
                "answer": f"The query could not be executed. Error: {e}",
                "error": str(e),
            }

    def _route_after_execute(self, state: GenBIState) -> str:
        return "error" if state.get("error") else "ok"

    def _node_bi_interpret(self, state: GenBIState) -> dict:
        try:
            result = self.bi.interpret(
                state["question"],
                state.get("rows", []),
                provider=state.get("provider", "gemini"),
                model=state.get("model"),
            )
            answer = result["answer"]
            chart_spec = result["chart_spec"]
        except Exception as e:
            print(f"[bi_interpret] Structured output failed: {e}")
            traceback.print_exc()
            answer = f"(Could not generate narrative: {e})"
            chart_spec = {}

        history = list(state.get("turn_history", []))
        is_error_answer = (
            not answer
            or answer.startswith("(Could not generate")
            or state.get("error")
        )
        if not is_error_answer:
            history.append({"question": state["question"], "answer": answer})
            history = history[-MAX_TURN_HISTORY:]

        return {"answer": answer, "chart_spec": chart_spec, "turn_history": history}

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _format_recent_turns(self, history: list[TurnRecord]) -> str:
        if not history:
            return ""
        lines = []
        for turn in history:
            lines.append(f"Q: {turn['question']}")
            lines.append(f"A: {turn['answer']}")
        return "\n".join(lines)

    # ── Public run API ─────────────────────────────────────────────────────────

    def run(self, question: str, provider: str = "gemini", model: str | None = None,
            thread_id: str = "default") -> dict:
        config = {"configurable": {"thread_id": thread_id}}
        return self._graph.invoke(
            {"question": question, "provider": provider, "model": model},
            config=config,
        )

    def reset_thread(self, thread_id: str) -> None:
        config = {"configurable": {"thread_id": thread_id}}
        try:
            current = self._graph.get_state(config)
            if current and current.values:
                self._graph.update_state(config, {"turn_history": []})
        except Exception as e:
            print(f"[GenBIGraph] reset_thread({thread_id}) - nothing to reset: {e}")