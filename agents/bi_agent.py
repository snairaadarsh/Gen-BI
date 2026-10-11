"""
agents/bi_agent.py
-------------------
Interprets a raw SQL result set and converts it into:
  1. A business-readable narrative answer
  2. A chart specification (type, axes, series, title) the frontend can use
     to render a bar / line / pie chart or a KPI card, instead of always
     returning plain text.

Built as a LangChain chain using `with_structured_output()` so the chart
spec is returned as a validated Pydantic object rather than parsed from
free-form text.

Supports two LLM providers (controlled by the `provider` kwarg):
  - "gemini"     → Google Gemini 2.0 Flash (default)
  - "openrouter" → Any model via OpenRouter (requires OPENROUTER_API_KEY)
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field

from agents.llm_client import get_chat_model

BI_SYSTEM_PROMPT = """You are a senior business intelligence analyst interpreting query results for an executive audience.

You will be given:
1. The original business question
2. A raw result set (list of dicts) from the database

Your task: produce both a business narrative AND a chart specification describing how to visualise the result.

Narrative rules:
- State the answer DIRECTLY in the first sentence — lead with the number.
- Include all key figures from the result set.
- Add ONE line of useful context if the data supports it (e.g. trend direction, what the number means).
- Do NOT invent numbers, percentages, or comparisons that aren't in the result set.
- Do NOT just describe the table or list all rows — synthesize and interpret.
- If the result set is empty, say so clearly and suggest a reason (e.g. date range might be outside the data).
- Keep it under 4 sentences for simple queries; use a short bulleted list only if there are multiple ranked items to report.
- Write in plain English suitable for a non-technical business stakeholder.

Chart specification rules:
- chart_type "kpi" for a single scalar answer (one row, one key metric) — no axes needed.
- chart_type "bar" for categorical rankings / comparisons (e.g. top N products, revenue by region).
- chart_type "line" for time-series / trend data (e.g. monthly revenue, daily orders).
- chart_type "pie" for proportional breakdowns of a whole (e.g. share of orders by payment type) — use sparingly, only when there are 6 or fewer categories.
- chart_type "table" if the result doesn't naturally fit a chart (e.g. many free-text columns).
- x_key / y_key MUST be copied EXACTLY, character-for-character, from the "Available columns" list provided below — do not paraphrase, rename, abbreviate, or guess a column name. If you are unsure which column to use, re-read the Available columns list and pick the closest exact match.
- y_key MUST refer to a column containing numeric values (not text/date/id).
- Do NOT invent a chart_type that isn't supported. If unsure, use "table".
"""


class ChartSpec(BaseModel):
    """Structured chart specification consumed by the React/Recharts frontend."""
    chart_type: Literal["bar", "line", "pie", "kpi", "table"] = Field(
        description="The chart type best suited to visualise this result set."
    )
    title: str = Field(description="Short, human-readable chart title.")
    x_key: Optional[str] = Field(default=None, description="Column name for the X axis / category.")
    y_key: Optional[str] = Field(default=None, description="Column name for the Y axis / value.")
    series_label: Optional[str] = Field(default=None, description="Label for the value series, e.g. 'Revenue (BRL)'.")


class BiInterpretation(BaseModel):
    """Full structured output returned by the BI Agent."""
    narrative: str = Field(description="The business-readable narrative answer.")
    chart_spec: ChartSpec = Field(description="Chart specification for frontend rendering.")


class BiAgent:
    def __init__(self):
        self._chains: dict[tuple[str, str | None], object] = {}

    def interpret(
        self,
        question: str,
        result_rows: list[dict],
        provider: str = "gemini",
        model: str | None = None,
    ) -> dict:
        """
        Converts a raw result set into a business narrative + chart spec.

        Args:
            question:    Original natural-language question.
            result_rows: Rows returned by ExecuteAgent.
            provider:    "gemini" or "openrouter".
            model:       Optional model ID override.

        Returns:
            {
              "answer": str,                 # business narrative (back-compat key name)
              "chart_spec": {                # structured chart specification
                  "chart_type": "bar" | "line" | "pie" | "kpi" | "table",
                  "title": str,
                  "x_key": str | None,
                  "y_key": str | None,
                  "series_label": str | None,
              }
            }
        """
        if not result_rows:
            return {
                "answer": (
                    "The query returned no results. This may be because the date range "
                    "falls outside the available data, or no records match the criteria."
                ),
                "chart_spec": {
                    "chart_type": "table",
                    "title": "No results",
                    "x_key": None,
                    "y_key": None,
                    "series_label": None,
                },
            }

        chain = self._get_chain(provider, model)

        # Pass the full result set (up to 500 rows, which is the cap from execute agent)
        sample = result_rows[:500]
        result_json = json.dumps(sample, indent=2, default=str)

        prompt = (
            f"Business question: {question}\n\n"
            f"Query result ({len(result_rows)} row(s) total, showing up to 500):\n"
            f"{result_json}\n\n"
            f"Available columns: {list(sample[0].keys())}\n\n"
            f"Business answer + chart spec:"
        )

        try:
            result: BiInterpretation = chain.invoke({"input": prompt})
            chart_spec = self._validate_chart_spec(result.chart_spec.model_dump(), sample)
            return {
                "answer": result.narrative,
                "chart_spec": chart_spec,
            }
        except Exception as e:
            # Ollama (and other small local models) frequently produce malformed
            # tool calls / JSON when with_structured_output is used, causing
            # Pydantic validation to fail. Rather than propagating the error
            # (which would write a useless error string into turn_history and
            # poison subsequent multi-turn context), fall back to a plain-text
            # completion with no chart spec. The answer will still be coherent
            # and safe to store in history.
            print(f"[BiAgent] Structured output failed ({e}); falling back to plain-text completion.")
            from langchain_core.messages import SystemMessage, HumanMessage
            plain_llm = get_chat_model(provider=provider, model=model, temperature=0.1)
            resp = plain_llm.invoke([
                SystemMessage(content=BI_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            return {
                "answer": (resp.content or "").strip(),
                "chart_spec": {
                    "chart_type": "table",
                    "title": "Result",
                    "x_key": None,
                    "y_key": None,
                    "series_label": None,
                },
            }

    @staticmethod
    def _validate_chart_spec(spec: dict, sample: list[dict]) -> dict:
        """
        Smaller / local models sometimes don't copy x_key / y_key exactly
        from the available columns despite instructions to do so. Rather
        than letting the frontend silently drop the chart in that case,
        auto-correct here: snap to the closest matching real column name,
        and pick a sensible numeric column for y_key if none was given.
        """
        if not sample:
            return spec

        columns = list(sample[0].keys())

        def closest_column(key):
            if key in columns:
                return key
            if not key:
                return None
            key_lower = str(key).lower().replace(" ", "_")
            for c in columns:
                if c.lower() == key_lower:
                    return c
            for c in columns:
                if key_lower in c.lower() or c.lower() in key_lower:
                    return c
            return None

        def is_numeric(col):
            val = sample[0].get(col)
            return isinstance(val, (int, float)) and not isinstance(val, bool)

        spec["x_key"] = closest_column(spec.get("x_key"))
        spec["y_key"] = closest_column(spec.get("y_key"))

        # If y_key still isn't numeric, fall back to the first numeric
        # column in the result set so the chart can still render.
        if not spec["y_key"] or not is_numeric(spec["y_key"]):
            numeric_cols = [c for c in columns if is_numeric(c)]
            spec["y_key"] = numeric_cols[0] if numeric_cols else spec["y_key"]

        # If x_key is missing, use the first non-numeric column as the
        # category axis (falls back to first column if all are numeric).
        if not spec["x_key"]:
            non_numeric_cols = [c for c in columns if not is_numeric(c)]
            spec["x_key"] = non_numeric_cols[0] if non_numeric_cols else columns[0]

        return spec

    # ── Internal ───────────────────────────────────────────────────────────────

    def _get_chain(self, provider: str, model: str | None):
        key = (provider, model)
        if key not in self._chains:
            from langchain_core.prompts import ChatPromptTemplate

            llm = get_chat_model(provider=provider, model=model, temperature=0.1)

            # Local Ollama models are unreliable with the default
            # function/tool-calling method LangChain uses for structured
            # output — small 7-8B models frequently produce malformed tool
            # calls, which silently fails upstream (graph.py catches the
            # exception and falls back to an empty chart_spec). Ollama's
            # native JSON-schema-constrained decoding is far more reliable
            # for this model size, so use it specifically for that provider.
            if provider == "ollama":
                structured_llm = llm.with_structured_output(BiInterpretation, method="json_schema")
            else:
                structured_llm = llm.with_structured_output(BiInterpretation)

            prompt_template = ChatPromptTemplate.from_messages([
                ("system", BI_SYSTEM_PROMPT),
                ("human", "{input}"),
            ])
            self._chains[key] = prompt_template | structured_llm
        return self._chains[key]
