"""
agents/powerbi_exporter.py
---------------------------
Generates a 100% valid Power BI Project (.pbip) folder zipped up for download.
Power BI Desktop opens this project cleanly with pre-drawn visuals and charts.
"""
from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from typing import Literal

from agents.dashboard_agent import DashboardSpec


# ── PBIP JSON Templates ────────────────────────────────────────────────────────

_PBIP_ENTRY = """{
  "version": "1.0",
  "artifacts": [
    {
      "report": {
        "path": "Report"
      }
    }
  ],
  "settings": {
    "enableAutoRecovery": true
  }
}"""

_PBIR_DEFINITION = """{
  "version": "4.0",
  "datasetReference": {
    "byPath": {
      "path": "../SemanticModel"
    }
  }
}"""

_PBISM_DEFINITION = """{
  "version": "1.0"
}"""



_EXPORTER_AGENT_PROMPT = """You are a senior Power BI Solutions Architect. Your task is to design a professional, enterprise-grade Power BI dashboard — including branding, layout, and theme — for an exported PBIP report.

Dashboard specifications:
Title: {title}
Number of Panels: {panel_count}

Panels:
{panels_info}

Your decisions:

1. CANVAS: Choose width & height. Standard: 1280×900. If >4 visuals, increase height to 1200–1600.
   IMPORTANT: Reserve the top 130px for the title/subtitle header bar. All chart panels must start at y >= 140.

2. BRANDING:
   - dashboard_title: Short, professional dashboard title (max 8 words). Make it descriptive and specific to the data, not generic.
   - dashboard_subtitle: One informative sentence (max 20 words) summarising what this report covers.
   - page_name: Report page tab name (max 4 words). E.g. "Executive Summary", "Revenue Overview".
   - panel_labels: Map each panel_id to a clean, human-readable label (Title Case, no underscores, no raw SQL column names). Make labels specific and meaningful.

3. LAYOUT: Assign exact (x, y, width, height) for each panel_id:
   - KPI/card panels: y=150, stacked horizontally. Height ~100px.
   - Chart panels (bar, line, pie): y >= 270, width ~580, height ~300.
   - Table panels: width ~1160, height ~320.
   - At least 20px gap between panels. No overlaps.

4. THEME: Premium color palette:
   - pageBackground: Dark slate, charcoal, or navy.
   - cardBackground: Slightly lighter than page background.
   - cardBorder: Subtle border color.
   - titleColor: Bright/white for title text.
   - subtitleColor: Muted/grey for subtitle text.
   - dataColors: 6–8 distinct vibrant hex colors for charts.

Output ONLY a strict JSON object. No markdown, no extra text.

Required format:
{{
  "canvas": {{ "width": 1280, "height": 1000 }},
  "dashboard_title": "E-Commerce Revenue Overview",
  "dashboard_subtitle": "Analysis of customer orders, category performance and review scores",
  "page_name": "Executive Summary",
  "panel_labels": {{
    "panel-uuid-1": "Total Revenue (BRL)",
    "panel-uuid-2": "Monthly Sales Trend"
  }},
  "theme": {{
    "name": "CorporateDark",
    "pageBackground": "#0f172a",
    "cardBackground": "#1c2533",
    "cardBorder": "#334155",
    "titleColor": "#f1f5f9",
    "subtitleColor": "#94a3b8",
    "dataColors": ["#00e5ff", "#7c3aed", "#10b981", "#f59e0b", "#ef4444", "#3b82f6", "#ec4899", "#84cc16"]
  }},
  "layout": [
    {{ "panel_id": "panel-uuid-1", "x": 40, "y": 150, "width": 380, "height": 100 }}
  ]
}}
"""


# ── Main Exporter ──────────────────────────────────────────────────────────────

class PowerBIExporter:
    """
    Converts a DashboardSpec into a downloadable, valid Power BI Project (.pbip).
    Auto-populates report.json with serialized visual charts (bar, line, pie, card, table).
    """

    def __init__(
        self,
        spec: DashboardSpec,
        api_base_url: str = "http://localhost:8000",
        provider: str = "gemini",
        model: str | None = None,
        design_plan: dict | None = None
    ):
        self._spec = spec
        self._api_base = api_base_url.rstrip("/")
        self._name = self._safe_name(spec.title)
        self._provider = provider
        self._model = model
        self._design_plan = design_plan

    def export(
        self, format: Literal["pbip", "pbix", "pbit"] = "pbip"
    ) -> tuple[bytes, str, str]:
        """
        Generate the export. All options route to the valid .pbip ZIP folder
        to ensure zero corruption errors on open.
        """
        # Run design plan with LLM first if not pre-passed
        if not self._design_plan:
            self._design_plan = self._build_layout_and_theme_with_llm()

        buf = io.BytesIO()
        name = self._name

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            prefix = f"{name}/"

            # 1. Root entry project file
            zf.writestr(f"{prefix}{name}.pbip", _PBIP_ENTRY)

            # 2. Report definition pointing to semantic model
            zf.writestr(f"{prefix}Report/definition.pbir", _PBIR_DEFINITION)

            # 3. Report layout (populated with pre-configured visualizations)
            report_layout = self._build_report_layout()
            zf.writestr(f"{prefix}Report/report.json", json.dumps(report_layout, indent=2))

            # 4. Semantic model definition
            zf.writestr(f"{prefix}SemanticModel/definition.pbism", _PBISM_DEFINITION)

            # 5. Semantic model BIM (contains entire table schemas & in-memory M queries)
            model_bim = self._build_model_bim()
            zf.writestr(f"{prefix}SemanticModel/model.bim", json.dumps(model_bim, indent=2))

            # 6. Report theme JSON file
            theme_json = self._build_theme_json()
            zf.writestr(
                f"{prefix}Report/StaticResources/SharedResources/BaseThemes/CustomTheme.json",
                json.dumps(theme_json, indent=2)
            )

        buf.seek(0)
        
        if format == "pbix":
            filename = f"{name}_pbix_unzip_me.zip"
        elif format == "pbit":
            filename = f"{name}_pbit_unzip_me.zip"
        else:
            filename = f"{name}_pbip.zip"

        return buf.read(), filename, "application/zip"

    # ── Layout Builder ────────────────────────────────────────────────────────

    def _build_report_layout(self) -> dict:
        """Top-level report.json for PBIP format with title/subtitle header and auto-visual layout."""
        canvas_info = self._design_plan["canvas"]
        canvas_w = canvas_info.get("width", 1280)
        canvas_h = canvas_info.get("height", 900)

        # Build lookup tables
        layout_lookup = {item["panel_id"]: item for item in self._design_plan.get("layout", [])}
        panel_labels = self._design_plan.get("panel_labels", {})

        # ── Title & subtitle header visuals ───────────────────────────────────
        dashboard_title = self._design_plan.get("dashboard_title") or self._spec.title or "Dashboard"
        dashboard_subtitle = self._design_plan.get("dashboard_subtitle", "")
        visuals = list(self._build_title_visuals(dashboard_title, dashboard_subtitle, canvas_w))

        # ── Data visuals ──────────────────────────────────────────────────────
        col_width = 560
        row_height = 300
        padding = 20
        header_offset = 140  # px reserved for the title/subtitle bar

        for i, panel in enumerate(self._spec.panels):
            layout_item = layout_lookup.get(panel.id)
            if layout_item:
                x = layout_item.get("x", 20)
                y = layout_item.get("y", 20)
                w = layout_item.get("width", 560)
                h = layout_item.get("height", 300)
            else:
                col = i % 2
                row = i // 2
                x = padding + col * (col_width + padding)
                y = header_offset + padding + row * (row_height + padding)
                w = col_width
                h = row_height

            visual = self._build_visual(panel, x, y, w, h, panel_labels=panel_labels)
            visuals.append(visual)

        # ── Report config ─────────────────────────────────────────────────────
        page_name = self._design_plan.get("page_name", "Dashboard Overview")
        config_obj = {
            "version": "5.60",
            "themeCollection": {
                "baseTheme": {"name": "CustomTheme", "version": "5.60", "type": 2}
            }
        }

        return {
            "config": json.dumps(config_obj),
            "layoutOptimization": 0,
            "sections": [
                {
                    "config": "{}",
                    "displayName": page_name,
                    "filters": "[]",
                    "height": float(canvas_h),
                    "name": "ReportSection",
                    "width": float(canvas_w),
                    "visualContainers": visuals
                }
            ]
        }

    def _build_visual(self, panel, x: int, y: int, w: int, h: int, panel_labels: dict | None = None) -> dict:
        """
        Constructs a correctly-formatted Power BI visual container for report.json.
        Numeric columns are wrapped in Sum() aggregation so Power BI Desktop
        automatically binds fields to the visual without manual intervention.
        """
        table_name = self._safe_name(panel.title)
        chart_type = panel.chart_spec.get("chart_type", "table")

        rows = panel.rows_data
        cols = list(rows[0].keys()) if rows else []

        x_key = panel.chart_spec.get("x_key", "") or ""
        y_key = panel.chart_spec.get("y_key", "") or ""

        # Fallback: pick first col as x, last numeric as y
        if x_key not in cols and cols:
            x_key = cols[0]
        if y_key not in cols and cols:
            y_key = cols[-1] if len(cols) > 1 else cols[0]

        # ── Helpers ───────────────────────────────────────────────────────────

        def is_numeric(col: str) -> bool:
            """Check if column contains numeric values (including string-encoded numbers)."""
            if not rows or col not in rows[0]:
                return False
            val = rows[0][col]
            if isinstance(val, bool):
                return False
            if isinstance(val, (int, float)):
                return True
            try:
                float(str(val).strip())
                return True
            except (ValueError, TypeError):
                return False

        def col_ref(col: str) -> str:
            """Raw column queryRef (for category/dimension columns)."""
            return f"{table_name}.{col}"

        def sum_ref(col: str) -> str:
            """Sum-aggregated queryRef (for numeric/measure columns)."""
            return f"Sum({table_name}.{col})"

        def col_select(col: str) -> dict:
            """Direct column Select expression (dimension)."""
            return {
                "Column": {
                    "Expression": {"SourceRef": {"Source": "t"}},
                    "Property": col
                },
                "Name": col_ref(col)
            }

        def agg_select(col: str) -> dict:
            """Sum-aggregated Select expression (measure). Function 0 = Sum."""
            return {
                "Aggregation": {
                    "Expression": {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": "t"}},
                            "Property": col
                        }
                    },
                    "Function": 0
                },
                "Name": sum_ref(col)
            }

        def smart_select(col: str) -> dict:
            return agg_select(col) if is_numeric(col) else col_select(col)

        def smart_ref(col: str) -> str:
            return sum_ref(col) if is_numeric(col) else col_ref(col)

        # ── Visual type + projections ─────────────────────────────────────────

        from_clause = [{"Name": "t", "Entity": table_name, "Type": 0}]

        if chart_type == "kpi":
            pbi_type = "card"
            projections = {"Values": [{"queryRef": smart_ref(y_key)}]}
            select_expressions = [smart_select(y_key)]

        elif chart_type == "bar":
            pbi_type = "barChart"
            projections = {
                "Category": [{"queryRef": col_ref(x_key)}],
                "Y": [{"queryRef": smart_ref(y_key)}]
            }
            select_expressions = [col_select(x_key), smart_select(y_key)]

        elif chart_type == "line":
            pbi_type = "lineChart"
            projections = {
                "Category": [{"queryRef": col_ref(x_key)}],
                "Y": [{"queryRef": smart_ref(y_key)}]
            }
            select_expressions = [col_select(x_key), smart_select(y_key)]

        elif chart_type == "pie":
            pbi_type = "pieChart"
            projections = {
                "Category": [{"queryRef": col_ref(x_key)}],
                "Y": [{"queryRef": smart_ref(y_key)}]
            }
            select_expressions = [col_select(x_key), smart_select(y_key)]

        else:  # table
            pbi_type = "tableEx"
            projections = {"Values": [{"queryRef": smart_ref(c)} for c in cols]}
            select_expressions = [smart_select(c) for c in cols]

        # ── Assemble config and query objects ────────────────────────────────

        guid = str(uuid.uuid4()).replace("-", "")[:20].upper()

        config_obj = {
            "name": guid,
            "layouts": [{
                "id": 0,
                "position": {"x": x, "y": y, "z": 0, "width": w, "height": h}
            }],
            "singleVisual": {
                "visualType": pbi_type,
                "projections": projections,
                "prototypeQuery": {
                    "Version": 2,
                    "From": from_clause,
                    "Select": select_expressions
                },
                "vcObjects": {},
                "title": {
                    "show": True,
                    "text": (panel_labels or {}).get(panel.id) or self._clean_label(panel.title)
                }
            }
        }

        query_obj = {
            "Commands": [{
                "SemanticQuery": {
                    "Version": 2,
                    "From": from_clause,
                    "Select": select_expressions
                }
            }]
        }

        return {
            "x": float(x),
            "y": float(y),
            "z": 0.0,
            "width": float(w),
            "height": float(h),
            "config": json.dumps(config_obj),
            "query": json.dumps(query_obj)
        }

    # ── Model Builder ─────────────────────────────────────────────────────────

    def _build_model_bim(self) -> dict:
        """
        Build the model.bim schema with tables, columns, and data partitions.
        Static rows are inlined directly using M query #table constructors.
        """
        tables = []
        for panel in self._spec.panels:
            table_name = self._safe_name(panel.title)
            rows = panel.rows_data[:500]
            if not rows:
                continue

            cols = list(rows[0].keys())

            # ── Column definitions with proper summarizeBy for numeric columns ──
            # Without summarizeBy="sum", Power BI Desktop treats numeric columns as
            # dimensions (text), causing visuals to show (Blank) instead of values.
            col_type_map = {col: self._infer_type(rows, col) for col in cols}
            columns = []
            for col in cols:
                dtype = col_type_map[col]
                col_def = {"name": col, "dataType": dtype}
                if dtype in ("int64", "double"):
                    col_def["summarizeBy"] = "sum"
                    col_def["annotations"] = [{"name": "SummarizationSetBy", "value": "Automatic"}]
                else:
                    col_def["summarizeBy"] = "none"
                columns.append(col_def)

            # ── M expression: simple #table() + TransformColumnTypes ─────────
            # Avoids the fragile "type table [...]" syntax which causes M engine
            # errors like 'Invalid identifier' and 'Token expected' in Power BI.
            # Instead we: (1) create a plain text table, (2) cast column types.
            def m_pq_type(dtype: str) -> str:
                """Map our internal dataType to a Power Query primitive type."""
                return {"int64": "type number", "double": "type number",
                        "boolean": "type logical"}.get(dtype, "type text")

            col_headers = ", ".join(f'"{c}"' for c in cols)
            row_lines = []
            for r in rows:
                vals = ", ".join(self._to_m_val(r.get(c)) for c in cols)
                row_lines.append(f"        {{{vals}}}")
            rows_joined = ",\n".join(row_lines)

            # TransformColumnTypes list: {{"col", type number}, ...}
            type_transforms = ", ".join(
                f'{{"{c}", {m_pq_type(col_type_map[c])}}}'
                for c in cols
            )

            expression = (
                f"let\n"
                f"    Source = #table(\n"
                f"        {{{col_headers}}},\n"
                f"        {{\n"
                f"{rows_joined}\n"
                f"        }}\n"
                f"    ),\n"
                f"    Typed = Table.TransformColumnTypes(Source, {{{type_transforms}}})\n"
                f"in\n"
                f"    Typed"
            )

            tables.append({
                "name": table_name,
                "columns": columns,
                "partitions": [
                    {
                        "name": "Partition",
                        "mode": "import",
                        "source": {
                            "type": "m",
                            "expression": expression
                        }
                    }
                ]
            })

        return {
            "name": self._name,
            "compatibilityLevel": 1550,
            "model": {
                "culture": "en-US",
                "defaultPowerBIDataSourceVersion": "powerBI_V3",
                "tables": tables
            }
        }

    def _build_theme_json(self) -> dict:
        """Construct a premium theme JSON matching the LLM-generated colors."""
        theme_data = self._design_plan["theme"]
        return {
            "name": theme_data.get("name", "CustomTheme"),
            "dataColors": theme_data.get("dataColors", [
                "#00e5ff", "#bcc7de", "#e4edff", "#00daf3", "#ffb4ab", "#3e495d", "#aeb9d0", "#c3f5ff"
            ]),
            "background": theme_data.get("pageBackground", "#0f172a"),
            "foreground": "#e4edff",
            "tableAccent": theme_data.get("dataColors", ["#00e5ff"])[0],
            "visualStyles": {
                "*": {
                    "*": {
                        "background": [
                            {
                                "show": True,
                                "color": { "solid": { "color": theme_data.get("cardBackground", "#1c2533") } },
                                "transparency": 0
                            }
                        ],
                        "border": [
                            {
                                "show": True,
                                "color": { "solid": { "color": theme_data.get("cardBorder", "#334155") } },
                                "radius": 8
                            }
                        ],
                        "dropShadow": [
                            {
                                "show": True,
                                "color": { "solid": { "color": "#000000" } },
                                "transparency": 60,
                                "preset": "BottomRight"
                            }
                        ]
                    }
                },
                "page": {
                    "*": {
                        "background": [
                            {
                                "show": True,
                                "color": { "solid": { "color": theme_data.get("pageBackground", "#0f172a") } },
                                "transparency": 0
                            }
                        ]
                    }
                }
            }
        }

    def _build_layout_and_theme_with_llm(self) -> dict:
        """Call the LLM to design custom canvas dimensions, coordinates, and theme settings."""
        from agents.llm_client import get_chat_model
        from langchain_core.messages import HumanMessage

        panels_list = []
        for p in self._spec.panels:
            cols_str = ""
            if p.rows_data:
                cols_str = ", ".join(list(p.rows_data[0].keys()))
            panels_list.append(
                f"- Panel ID: {p.id}\n"
                f"  Title: {p.title}\n"
                f"  Chart Type: {p.chart_spec.get('chart_type', 'table') if p.chart_spec else 'table'}\n"
                f"  Columns: {cols_str}\n"
                f"  Rows count: {len(p.rows_data)}"
            )
        panels_info = "\n".join(panels_list)

        prompt = _EXPORTER_AGENT_PROMPT.format(
            title=self._spec.title,
            panel_count=len(self._spec.panels),
            panels_info=panels_info
        )

        try:
            llm = get_chat_model(provider=self._provider, model=self._model, temperature=0.2)
            resp = llm.invoke([HumanMessage(content=prompt)])
            raw_content = (resp.content or "").strip()

            if raw_content.startswith("```json"):
                raw_content = raw_content[7:]
            if raw_content.startswith("```"):
                raw_content = raw_content[3:]
            if raw_content.endswith("```"):
                raw_content = raw_content[:-3]
            raw_content = raw_content.strip()

            design_plan = json.loads(raw_content)

            if "canvas" in design_plan and "theme" in design_plan and "layout" in design_plan:
                return design_plan
            raise ValueError("LLM response missing required keys: 'canvas', 'theme', 'layout'")
        except Exception as e:
            raise RuntimeError(f"Power BI design agent failed to generate layout: {e}")

    # ── Title Visual Builder ──────────────────────────────────────────────────

    def _build_title_visuals(self, title: str, subtitle: str, canvas_w: int) -> list[dict]:
        """
        Build title + subtitle as native PBIP textbox visuals at the top of the canvas.
        These render immediately in Power BI Desktop without any manual intervention.
        """
        theme_data = self._design_plan.get("theme", {})
        title_color = theme_data.get("titleColor", "#f1f5f9")
        subtitle_color = theme_data.get("subtitleColor", "#94a3b8")
        accent = theme_data.get("dataColors", ["#00e5ff"])[0]

        def _textbox(text: str, x: int, y: int, w: int, h: int,
                     font_size: str, color: str, bold: bool) -> dict:
            guid = re.sub(r"[^A-Z0-9]", "", str(uuid.uuid4()).upper())[:20]
            paragraphs = [{
                "textRuns": [{
                    "value": text,
                    "textStyle": {
                        "bold": "true" if bold else "false",
                        "fontSize": font_size,
                        "fontFamily": "Segoe UI",
                        "color": {"solid": {"color": color}}
                    }
                }],
                "horizontalTextAlignment": "Left",
                "spacingBefore": 0,
                "spacingAfter": 0
            }]
            cfg = {
                "name": guid,
                "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 200, "width": w, "height": h}}],
                "singleVisual": {
                    "visualType": "textbox",
                    "vcObjects": {"general": [{"properties": {"paragraphs": paragraphs}}]}
                }
            }
            return {
                "x": float(x), "y": float(y), "z": 200.0,
                "width": float(w), "height": float(h),
                "config": json.dumps(cfg),
                "query": "{}", "dataTransforms": "{}"
            }

        visuals = []
        # Title row
        visuals.append(_textbox(title, 24, 12, canvas_w - 48, 60, "20pt", title_color, bold=True))
        # Subtitle row
        if subtitle:
            visuals.append(_textbox(subtitle, 24, 74, canvas_w - 48, 36, "10pt", subtitle_color, bold=False))
        # Accent divider line (thin 2px textbox used as a visual separator)
        sep_guid = re.sub(r"[^A-Z0-9]", "", str(uuid.uuid4()).upper())[:20]
        sep_cfg = {
            "name": sep_guid,
            "layouts": [{"id": 0, "position": {"x": 0, "y": 118, "z": 200, "width": canvas_w, "height": 4}}],
            "singleVisual": {
                "visualType": "textbox",
                "background": {"show": True, "color": {"solid": {"color": accent}}, "transparency": 0},
                "vcObjects": {"general": [{"properties": {"paragraphs": []}}]}
            }
        }
        visuals.append({"x": 0.0, "y": 118.0, "z": 200.0, "width": float(canvas_w), "height": 4.0,
                        "config": json.dumps(sep_cfg), "query": "{}", "dataTransforms": "{}"})
        return visuals

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _clean_label(raw: str) -> str:
        """Convert snake_case/raw column names to human-readable Title Case labels."""
        text = re.sub(r"[_\-]+", " ", raw or "")
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # camelCase split
        return " ".join(w.capitalize() for w in text.strip().split())

    @staticmethod
    def _safe_name(title: str) -> str:
        """Convert a title to a safe filename/table name."""
        name = re.sub(r"[^\w\s-]", "", title)
        name = re.sub(r"[\s-]+", "_", name).strip("_")
        return name[:50] or "Dashboard"

    @staticmethod
    def _infer_type(rows: list[dict], col: str) -> str:
        """Infer Power BI data type string from sample data, with numeric string conversion support."""
        val = rows[0].get(col) if rows else None
        if val is None:
            return "string"
        if isinstance(val, bool):
            return "boolean"
        if isinstance(val, int):
            return "int64"
        if isinstance(val, float):
            return "double"
        
        # Check if the string format represents a valid integer or float
        val_str = str(val).strip()
        if re.match(r"^-?\d+$", val_str):
            return "int64"
        if re.match(r"^-?\d*\.\d+$", val_str):
            return "double"
        return "string"

    @staticmethod
    def _to_m_val(val) -> str:
        """Format a python value into a valid M language cell literal, writing numeric strings without quotes."""
        if val is None:
            return "null"
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, (int, float)):
            return str(val)
            
        # If it is a string representing a number, write it without quotes
        val_str = str(val).strip()
        if re.match(r"^-?\d+(\.\d+)?$", val_str):
            return val_str
            
        escaped = val_str.replace('"', '""')
        return f'"{escaped}"'
