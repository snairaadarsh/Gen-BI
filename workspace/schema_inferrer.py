"""
workspace/schema_inferrer.py
-----------------------------
Uses an LLM to infer human-readable column descriptions and table descriptions
from raw SQL column names + dtypes, then produces a catalog.yaml compatible
with the existing MetadataAgent.

Output catalog.yaml structure mirrors metadata/catalog.yaml exactly so all
downstream agents work without modification.

FIX #16: Removed sys.path.insert() from inside _call_llm(). The project root
is placed on sys.path by api/main.py at startup; normal absolute imports work.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import yaml

from agents.llm_client import get_chat_model


# ── LLM call ──────────────────────────────────────────────────────────────────

def _call_llm(prompt: str, provider: str = "gemini", model: str | None = None) -> str:
    """Thin wrapper around the existing LLM client."""
    llm = get_chat_model(provider=provider, model=model)
    response = llm.invoke(prompt)
    return response.content if hasattr(response, "content") else str(response)


# ── Prompt ─────────────────────────────────────────────────────────────────────

def _build_prompt(tables: dict) -> str:
    table_blocks = []
    for tname, info in tables.items():
        cols = "\n".join(
            f"  - {col} ({dtype})"
            for col, dtype in info["columns"].items()
        )
        table_blocks.append(
            f"Table: {tname} ({info.get('row_count', '?')} rows)\nColumns:\n{cols}"
        )

    tables_text = "\n\n".join(table_blocks)

    return f"""You are a data catalog assistant. Given a set of database tables and their
column names + data types, produce a JSON schema catalog describing each table and column
in business-friendly language, and suggest a short business glossary.

Tables:
{tables_text}

Respond ONLY with a valid JSON object — no markdown, no explanation, no backticks.
The JSON must match this exact structure:

{{
  "tables": {{
    "<table_name>": {{
      "description": "<one-sentence business description of this table>",
      "columns": {{
        "<column_name>": "<plain English description of what this column contains>"
      }},
      "joins": []
    }}
  }},
  "glossary": {{
    "<business term>": "<definition>",
    "<business term>": "<definition>"
  }}
}}

Guidelines:
- Use business language, not technical jargon.
- For columns like order_id, customer_id — describe what entity they identify.
- For date/timestamp columns — describe what event they record.
- For numeric columns — describe what they measure and the likely unit.
- Suggest 5–10 glossary terms that a business analyst would find useful given these tables.
- If you can infer likely joins between tables (e.g. a shared _id column), add them as
  strings in the "joins" array like: "JOIN other_table ON this.col = other_table.col"
"""


# ── Main public function ───────────────────────────────────────────────────────

def infer_schema(
    tables: dict,
    provider: str = "gemini",
    model: str | None = None,
) -> dict:
    """
    Call the LLM to infer a catalog dict from the tables summary produced by
    csv_ingestor.ingest_csvs() or db_connector.reflect_schema().

    Args:
        tables: { table_name: { "columns": {col: dtype}, "row_count": int, ... } }
        provider / model: forwarded to the LLM client

    Returns:
        catalog dict matching catalog.yaml structure:
        { "tables": { ... }, "glossary": { ... } }
    """
    prompt  = _build_prompt(tables)
    raw     = _call_llm(prompt, provider=provider, model=model)
    catalog = _parse_llm_response(raw, tables)
    return catalog


def _parse_llm_response(raw: str, tables: dict) -> dict:
    """Parse LLM JSON response. Raises ValueError if the response cannot be parsed."""
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        data = json.loads(cleaned)
        if "tables" not in data:
            raise ValueError("Missing 'tables' key in LLM response")
        return data
    except Exception as e:
        raise ValueError(
            f"[schema_inferrer] LLM returned invalid JSON — cannot infer schema. "
            f"Error: {e}. Raw response (first 500 chars): {raw[:500]!r}"
        ) from e


def _fallback_catalog(tables: dict) -> dict:
    """
    Build a minimal catalog from raw column names when LLM output fails.
    """
    catalog_tables: dict[str, Any] = {}
    for tname, info in tables.items():
        catalog_tables[tname] = {
            "description": f"Data from table '{tname}'.",
            "columns": {
                col: f"{col.replace('_', ' ').title()} ({dtype})"
                for col, dtype in info["columns"].items()
            },
            "joins": [],
        }
    return {"tables": catalog_tables, "glossary": {}}


# ── Persist to YAML ────────────────────────────────────────────────────────────

def save_catalog(catalog: dict, catalog_path: str) -> None:
    """Write the catalog dict to a catalog.yaml file."""
    os.makedirs(os.path.dirname(catalog_path), exist_ok=True)
    with open(catalog_path, "w") as f:
        yaml.dump(catalog, f, allow_unicode=True, sort_keys=False)
    print(f"[schema_inferrer] Catalog saved -> {catalog_path}")


def save_certified_reports(reports_path: str) -> None:
    """Write an empty certified_reports.yaml for a new workspace."""
    os.makedirs(os.path.dirname(reports_path), exist_ok=True)
    if not os.path.exists(reports_path):
        with open(reports_path, "w") as f:
            yaml.dump({"reports": []}, f)
        print(f"[schema_inferrer] Empty certified_reports created -> {reports_path}")