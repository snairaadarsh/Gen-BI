"""
agents/sql_agent.py
--------------------
Converts a natural-language business question to a SQL query using a
LangChain prompt | llm | StrOutputParser chain.

Self-healing: if the Execute Agent reports that the generated SQL failed,
the orchestrator can call `generate_with_self_heal()` which feeds the DB
error back into the prompt and retries up to MAX_SELF_HEAL_ATTEMPTS times.

Supports three LLM providers (controlled by the `provider` kwarg):
  - "gemini"     → Google Gemini 2.0 Flash (default)
  - "openrouter" → Any model via OpenRouter (requires OPENROUTER_API_KEY)
  - "ollama"     → Local Ollama instance

FIX #12: Removed the redundant re-embedding of the system prompt inside
the human message in generate(). The system prompt is injected once via
ChatPromptTemplate; the human message now contains only the context +
question + optional self-heal suffix. This avoids sending the system
instructions twice and reduces token usage.
"""
from __future__ import annotations

import re
import os
import traceback

from agents.llm_client import get_chat_model

MAX_SELF_HEAL_ATTEMPTS = 3


def get_system_prompt(dialect: str = "sqlite") -> str:
    dialect_upper = dialect.upper() if dialect else "SQLITE"
    dialect = dialect.lower() if dialect else "sqlite"

    if dialect == "sqlite":
        dialect_rules = """- Use SQLite-compatible syntax:
  * Dates: strftime('%Y', col), strftime('%m', col), strftime('%Y-%m', col)
  * NEVER use EXTRACT(), DATE_TRUNC(), TO_DATE(), or any PostgreSQL/MySQL functions.
  * Date arithmetic: date(col, '-N months'), date(col, '-N days').
  * Relative date filtering: Anchor relative to the latest date in the data.
    Example: WHERE col >= date((SELECT MAX(col) FROM table), '-6 months')"""
    elif dialect == "postgresql":
        dialect_rules = """- Use PostgreSQL-compatible syntax:
  * Dates: to_char(col, 'YYYY'), to_char(col, 'MM'), to_char(col, 'YYYY-MM'),
    or EXTRACT(YEAR FROM col), EXTRACT(MONTH FROM col).
  * Date arithmetic: col - INTERVAL 'N months', col - INTERVAL 'N days'.
  * Relative date filtering: Anchor relative to the latest date in the data.
    Example: WHERE col >= (SELECT MAX(col) FROM table) - INTERVAL '6 months'"""
    elif dialect == "mysql":
        dialect_rules = """- Use MySQL-compatible syntax:
  * Dates: DATE_FORMAT(col, '%Y'), DATE_FORMAT(col, '%m'), DATE_FORMAT(col, '%Y-%m'),
    or YEAR(col), MONTH(col).
  * Date arithmetic: DATE_SUB(col, INTERVAL N MONTH), DATE_SUB(col, INTERVAL N DAY).
  * Relative date filtering: Anchor relative to the latest date in the data.
    Example: WHERE col >= DATE_SUB((SELECT MAX(col) FROM table), INTERVAL 6 MONTH)"""
    elif dialect == "mssql":
        dialect_rules = """- Use Microsoft SQL Server (MSSQL)-compatible syntax:
  * Dates: FORMAT(col, 'yyyy'), FORMAT(col, 'MM'), FORMAT(col, 'yyyy-MM'),
    or YEAR(col), MONTH(col).
  * Date arithmetic: DATEADD(month, -N, col), DATEADD(day, -N, col).
  * Relative date filtering: Anchor relative to the latest date in the data.
    Example: WHERE col >= DATEADD(month, -6, (SELECT MAX(col) FROM table))"""
    else:
        dialect_rules = f"""- Use standard {dialect_upper}-compatible SQL syntax.
  * Dates: Use appropriate format and parsing functions for {dialect_upper}.
  * Date arithmetic: Use appropriate interval functions for {dialect_upper}.
  * Relative date filtering: Anchor relative to the latest date in the data."""

    return f"""You are a SQL generation agent for a business intelligence system built on {dialect_upper}.

You will be given:
1. A business question from a non-technical user
2. Relevant table/column documentation and a business glossary extracted from the actual database schema

Your task: Generate a single, syntactically correct {dialect_upper} query that precisely answers the question.

Rules:
- Use ONLY the table and column names that appear in the context below. Do NOT invent, guess, or hallucinate any table or column names.
- If a table or column is not explicitly listed in the context, do not use it.
- If a business term appears in the glossary, use the EXACT field it maps_to.
{dialect_rules}
- Do NOT add any date range filters unless the user's question explicitly asks for a time period.
- Never use SQL reserved keywords (like 'or', 'and', 'select', 'join', 'order') as table aliases.
- Return ONLY the raw SQL query — no markdown, no code fences, no explanation.
- Always add meaningful column aliases (AS revenue, AS total_orders, etc.).
- Do NOT add any LIMIT clauses to the query unless the user's question explicitly asks for a limit (e.g. "top 5", "first 3") or implies a specific count.
- For "top N" queries, always use appropriate dialect syntax (e.g., LIMIT N for SQLite/PostgreSQL/MySQL, TOP N for MSSQL) with ORDER BY ... DESC.
"""

SQL_SELF_HEAL_SUFFIX = """

IMPORTANT — SELF-CORRECTION:
A previous attempt at this query failed when executed against the database.
Previous SQL:
{previous_sql}

Database error:
{db_error}

Carefully analyse the error and the context above, then return a corrected
{dialect} query that fixes the problem. Return ONLY the raw corrected SQL —
no markdown, no explanation.
"""


class SqlAgent:
    def __init__(self, dialect: str = "sqlite"):
        self.dialect = dialect.lower() if dialect else "sqlite"
        # LangChain chains are built lazily per (provider, model, dialect) the first
        # time generate() is called, then cached for reuse.
        self._chains: dict[tuple[str, str | None, str], object] = {}

    def generate(
        self,
        question: str,
        context: str,
        provider: str = "gemini",
        model: str | None = None,
        previous_sql: str | None = None,
        db_error: str | None = None,
    ) -> str:
        """
        Generates a SQL query for the given question using the catalog context.
        Returns clean SQL (no markdown fences).

        FIX #12: The human message now contains only context + question (+ optional
        self-heal suffix). The system prompt lives exclusively in the
        ChatPromptTemplate system message, so it is not sent twice.

        Args:
            question:     Natural-language business question.
            context:      Catalog context from MetadataAgent.
            provider:     "gemini", "openrouter", or "ollama".
            model:        Optional model ID override.
            previous_sql: If set, this is a self-healing retry — the SQL that failed.
            db_error:     The database error message from the failed attempt.
        """
        chain = self._get_chain(provider, model)

        # Human message: context + question only (system prompt is in the template).
        prompt = (
            f"Context (table docs + glossary):\n{context}\n\n"
            f"Business question: {question}\n\n"
            f"SQL query:"
        )
        if previous_sql and db_error:
            prompt += SQL_SELF_HEAL_SUFFIX.format(
                previous_sql=previous_sql,
                db_error=db_error,
                dialect=self.dialect.upper(),
            )

        raw = chain.invoke({"input": prompt})
        return self._strip_code_fences(raw)

    def generate_with_self_heal(
        self,
        question: str,
        context: str,
        execute_fn,
        provider: str = "gemini",
        model: str | None = None,
        max_attempts: int = MAX_SELF_HEAL_ATTEMPTS,
    ) -> tuple[str, list[dict]]:
        """
        Generates SQL and executes it via execute_fn, automatically retrying
        with the database error fed back into the prompt on failure.

        Args:
            question:     Natural-language business question.
            context:      Catalog context from MetadataAgent.
            execute_fn:   Callable(sql: str) -> list[dict]; raises on failure.
            provider:     LLM provider string.
            model:        Optional model ID override.
            max_attempts: Maximum total attempts (initial + retries).

        Returns:
            (final_sql, rows) on success.

        Raises:
            The last execution exception if all attempts are exhausted.
        """
        previous_sql = None
        db_error = None
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            sql = self.generate(
                question, context, provider=provider, model=model,
                previous_sql=previous_sql, db_error=db_error,
            )
            try:
                rows = execute_fn(sql)
                return sql, rows
            except Exception as e:
                last_exc = e
                previous_sql = sql
                db_error = str(e)
                print(f"[SqlAgent] Self-heal attempt {attempt}/{max_attempts} failed: {e}")

        raise last_exc

    # ── Internal ───────────────────────────────────────────────────────────────

    def _get_chain(self, provider: str, model: str | None):
        key = (provider, model, self.dialect)
        if key not in self._chains:
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_core.output_parsers import StrOutputParser

            llm = get_chat_model(provider=provider, model=model, temperature=0.0)
            system_prompt = get_system_prompt(self.dialect)
            prompt_template = ChatPromptTemplate.from_messages([
                ("system", system_prompt),
                ("human", "{input}"),
            ])
            self._chains[key] = prompt_template | llm | StrOutputParser()
        return self._chains[key]

    def _strip_code_fences(self, text: str) -> str:
        """Removes ```sql ... ``` or ``` ... ``` wrapping."""
        text = re.sub(r"^```(?:sql)?\s*\n?", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()