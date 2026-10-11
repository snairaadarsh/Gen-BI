"""
agents/execute_agent.py
------------------------
Executes a SQL query against the database defined by DATABASE_URL.
Safety net: rejects anything that isn't a SELECT before it touches the DB.

This module now also exposes the same execution logic as a LangChain
`Tool` (`get_sql_execution_tool()`), so it can be bound to a LangChain
agent or LangGraph node as a first-class tool call instead of only being
invoked as a plain Python method. The underlying ExecuteAgent class and
its `.run()` / `.test_connection()` methods are unchanged — the Tool is a
thin adapter on top, so existing callers (graph.py, sql_agent.py) keep
working exactly as before.

[UPGRADE] Constructor now accepts an optional `db_url` parameter.
If supplied, it is used directly (workspace-specific DB).
If omitted, falls back to the DATABASE_URL env var (backward compatible).
"""
import json
import os

from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

MAX_ROWS = int(os.environ.get("MAX_SQL_ROWS", "500"))  # cap result set for the demo


class ExecuteAgent:
    def __init__(self, db_url: str | None = None):
        if db_url is None:
            db_url = os.environ.get("DATABASE_URL")
            if db_url is None:
                raise ValueError("db_url or DATABASE_URL env var is required for ExecuteAgent.")

        # Resolve relative SQLite path relative to the project root
        if db_url.startswith("sqlite:///./"):
            relative_path = db_url[len("sqlite:///./"):]
            project_root = os.path.join(os.path.dirname(__file__), "..")
            abs_path = os.path.abspath(os.path.join(project_root, relative_path))
            db_url = f"sqlite:///{abs_path}"

        self.db_url = db_url
        self.engine = create_engine(db_url)

    def run(self, sql: str) -> list[dict]:
        """
        Executes the SQL and returns rows as a list of dicts.
        Only SELECT statements are permitted.
        """
        clean = sql.strip()
        if not clean.lower().startswith("select"):
            raise ValueError(
                f"Only SELECT statements are permitted. Got: {clean[:80]}..."
            )

        with self.engine.connect() as conn:
            result = conn.execute(text(clean))
            rows = [dict(row._mapping) for row in result]

        if len(rows) > MAX_ROWS:
            rows = rows[:MAX_ROWS]

        return rows

    def test_connection(self) -> bool:
        """Returns True if the database is reachable."""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ── LangChain Tool adapter ───────────────────────────────────────────────

    def as_langchain_tool(self):
        from langchain_core.tools import Tool

        def _run_and_serialize(sql: str) -> str:
            rows = self.run(sql)
            return json.dumps(rows, default=str)

        return Tool(
            name="execute_sql_query",
            description=(
                "Executes a read-only SQL SELECT statement against the GenBI "
                "database and returns the result rows as a JSON array of "
                "objects. Only SELECT statements are permitted; anything else "
                "raises an error. Input should be a single valid SQLite query "
                "string."
            ),
            func=_run_and_serialize,
        )


# ── Module-level convenience factory ─────────────────────────────────────────

_default_agent: ExecuteAgent | None = None


def get_sql_execution_tool():
    global _default_agent
    if _default_agent is None:
        _default_agent = ExecuteAgent()
    return _default_agent.as_langchain_tool()
