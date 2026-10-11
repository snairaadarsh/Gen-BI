"""
workspace/glossary_generator.py
---------------------------------
Generates a richer business glossary from an existing catalog dict.
Called after schema inference so the LLM has full table + column context.

The glossary produced here augments (not replaces) whatever the schema
inferrer already put in catalog["glossary"]. The user reviews and edits
it in Screen 4 before it's saved back.

FIX #16: Removed sys.path.insert() from inside _call_llm(). The project root
is placed on sys.path by api/main.py at startup; normal absolute imports work.
"""
from __future__ import annotations

import json
import re

from agents.llm_client import get_chat_model


def _call_llm(prompt: str, provider: str = "gemini", model: str | None = None) -> str:
    llm = get_chat_model(provider=provider, model=model)
    response = llm.invoke(prompt)
    return response.content if hasattr(response, "content") else str(response)


def _build_prompt(catalog: dict) -> str:
    lines = []
    for tname, info in catalog.get("tables", {}).items():
        lines.append(f"Table '{tname}': {info.get('description', '')}")
        for col, desc in info.get("columns", {}).items():
            lines.append(f"  - {col}: {desc}")

    existing_terms = list(catalog.get("glossary", {}).keys())
    existing_note  = (
        f"\nAlready defined terms (do NOT repeat): {', '.join(existing_terms)}"
        if existing_terms else ""
    )

    catalog_text = "\n".join(lines)

    return f"""You are a business intelligence glossary writer.
Given the following database schema (with descriptions), generate a business glossary
of 10–15 terms that a business analyst or executive would find useful when querying
this data.{existing_note}

Schema:
{catalog_text}

Respond ONLY with a valid JSON array — no markdown, no backticks.
Each item in the array must have exactly these fields:
  "term"       — the business term (2–5 words)
  "definition" — plain English definition (1–2 sentences)
  "maps_to"    — the most relevant table.column(s) this term maps to (or "" if N/A)

Example item:
{{"term": "Net Revenue", "definition": "Total sales revenue after refunds and discounts.", "maps_to": "orders.price"}}

Produce 10–15 items. Focus on KPIs, business concepts, and domain-specific terms
visible in the data — not just column name aliases.
"""


def generate_glossary(
    catalog: dict,
    provider: str = "gemini",
    model: str | None = None,
) -> list[dict]:
    """
    Generate a list of glossary term dicts from the catalog.

    Returns:
        [ { "term": str, "definition": str, "maps_to": str }, ... ]
    """
    prompt = _build_prompt(catalog)
    raw    = _call_llm(prompt, provider=provider, model=model)
    return _parse_response(raw, catalog)


def _parse_response(raw: str, catalog: dict) -> list[dict]:
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        items = json.loads(cleaned)
        if not isinstance(items, list):
            raise ValueError("Expected a JSON array")
        result = []
        for item in items:
            if not isinstance(item, dict) or "term" not in item:
                continue
            result.append({
                "term":       str(item.get("term", "")).strip(),
                "definition": str(item.get("definition", "")).strip(),
                "maps_to":    str(item.get("maps_to", "")).strip(),
            })
        return result
    except Exception as e:
        raise ValueError(
            f"[glossary_generator] LLM returned invalid JSON — cannot generate glossary. "
            f"Error: {e}. Raw response (first 500 chars): {raw[:500]!r}"
        ) from e


def _fallback_glossary(catalog: dict) -> list[dict]:
    """Minimal fallback: one entry per table."""
    return [
        {
            "term":       tname.replace("_", " ").title(),
            "definition": info.get("description", f"Data in the {tname} table."),
            "maps_to":    tname,
        }
        for tname, info in catalog.get("tables", {}).items()
    ]


def merge_glossary_into_catalog(catalog: dict, glossary_rows: list[dict]) -> dict:
    """
    Merge user-reviewed glossary rows back into the catalog dict's
    'glossary' section (term → definition mapping for MetadataAgent).
    """
    catalog = dict(catalog)
    catalog["glossary"] = {
        row["term"]: row["definition"]
        for row in glossary_rows
        if row.get("term")
    }
    return catalog