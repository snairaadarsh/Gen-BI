# GenBI — Generative Business Intelligence

> A conversational, AI-powered Business Intelligence platform that lets you upload any CSV or connect any SQL database and instantly start asking business questions in plain English — receiving SQL-backed answers, interactive charts, and exportable Power BI dashboards.

---

## 📋 Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Agent Pipeline Workflow](#agent-pipeline-workflow)
- [Project Structure](#project-structure)
- [Tech Stack](#tech-stack)
- [Setup & Installation](#setup--installation)
- [Configuration](#configuration)
- [Running the App](#running-the-app)
- [Usage Walkthrough](#usage-walkthrough)
- [Supported LLM Providers](#supported-llm-providers)
- [Power BI Export](#power-bi-export)
- [API Reference](#api-reference)

---

## Overview

GenBI is a **Generative BI proof-of-concept** that replaces rigid, pre-built dashboards with a conversational AI layer over your own data. You bring your data (CSV files or an external database), and GenBI handles the rest:

- Automatically profiles your schema with LLM-generated business descriptions
- Converts natural language questions into validated SQL queries
- Renders results as interactive bar, line, pie charts or KPI cards
- Auto-generates executive-level overview dashboards
- Exports complete, ready-to-open Power BI Project (`.pbip`) files

No data ever leaves your machine unless you choose a cloud LLM provider.

---

## Key Features

| Feature | Description |
|---------|-------------|
| 📁 **CSV & DB Ingestion** | Upload multiple CSVs or connect PostgreSQL, MySQL, SQL Server, or SQLite |
| 🧠 **LLM Schema Inference** | AI describes every table and column in plain business language |
| 📖 **Business Glossary** | AI generates a domain-specific glossary; human-in-the-loop review |
| 💬 **Conversational Query** | Ask questions in plain English; get SQL + chart + narrative |
| 🔁 **Self-Healing SQL** | Automatically retries and fixes broken SQL up to 3 times |
| 📊 **Auto Dashboard** | One-click executive dashboard with 6 panels (KPIs, charts, tables) |
| 📤 **Power BI Export** | Download a fully structured `.pbip` file that opens in Power BI Desktop |
| 🔒 **Local-First** | All data stays on-disk; use local Ollama models for full air-gap privacy |
| 🔑 **Multi-Provider LLM** | Swap between Gemini, OpenRouter (GPT-4o, Claude, Mistral), or Ollama |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Browser (index.html)                           │
│  ┌──────────┐  ┌────────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │   Chat   │  │ Schema Editor  │  │   Glossary   │  │  Dashboard   │ │
│  │ Interface│  │  (HITL Review) │  │   Editor     │  │   Builder    │ │
│  └────┬─────┘  └───────┬────────┘  └──────┬───────┘  └──────┬───────┘ │
└───────┼────────────────┼──────────────────┼─────────────────┼──────────┘
        │                │                  │                 │
        ▼                ▼                  ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     FastAPI Backend  (api/)                             │
│  ┌─────────────┐  ┌──────────────────┐  ┌───────────────────────────┐ │
│  │  POST /ask  │  │  /workspace/*    │  │  /dashboard/*  /export/*  │ │
│  │  (main.py)  │  │  (routes)        │  │  (main.py)                │ │
│  └──────┬──────┘  └────────┬─────────┘  └────────────┬──────────────┘ │
└─────────┼──────────────────┼─────────────────────────┼─────────────────┘
          │                  │                          │
          ▼                  ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     AI Agent Pipeline  (agents/)                        │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                    Orchestrator                                   │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │  │
│  │  │  Graph   │  │  RAG     │  │  SQL     │  │  BI Interpret  │  │  │
│  │  │(LangGraph│→ │  Agent   │→ │  Agent   │→ │  Agent         │  │  │
│  │  │workflow) │  │(vectors) │  │(NL→SQL)  │  │(chart+narrate) │  │  │
│  │  └──────────┘  └──────────┘  └────┬─────┘  └────────────────┘  │  │
│  │                                    │                              │  │
│  │                            ┌───────▼──────┐                      │  │
│  │                            │ Execute Agent│                      │  │
│  │                            │ (SQL runner) │                      │  │
│  │                            └──────────────┘                      │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐ │
│  │  Metadata Agent  │  │  Dashboard Agent │  │  PowerBI Exporter    │ │
│  │  (RAG over       │  │  (auto-propose   │  │  (.pbip generator)   │ │
│  │   catalog.yaml)  │  │   questions)     │  │                      │ │
│  └──────────────────┘  └──────────────────┘  └──────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
          │                                          │
          ▼                                          ▼
┌──────────────────────┐               ┌─────────────────────────────────┐
│  LLM Client          │               │  Workspace Storage (disk)       │
│  ┌────────────────┐  │               │                                 │
│  │ Gemini         │  │               │  workspaces/<uuid>/             │
│  │ OpenRouter     │  │               │  ├── data.db  (SQLite)          │
│  │ Ollama (local) │  │               │  ├── meta.json                  │
│  └────────────────┘  │               │  ├── catalog.yaml               │
└──────────────────────┘               │  ├── certified_reports.yaml     │
                                       │  └── faiss_index/               │
                                       └─────────────────────────────────┘
```

---

## Agent Pipeline Workflow

```
User asks: "What is the revenue trend in 2017?"
                            │
                            ▼
               ┌────────────────────────┐
               │      Orchestrator      │
               │  (workspace context    │
               │   + session history)   │
               └───────────┬────────────┘
                           │
                           ▼
               ┌────────────────────────┐
               │       RAG Agent        │◄── certified_reports.yaml
               │  (search pre-built     │    (vector similarity search)
               │   query templates)     │
               └───────────┬────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
       Match found?                  No match
              │                         │
              ▼                         ▼
    Return cached result      ┌─────────────────────┐
                              │    Metadata Agent    │◄── catalog.yaml
                              │  (RAG over schema,   │    FAISS index
                              │   glossary context)  │
                              └──────────┬───────────┘
                                         │  "Here are tables + columns"
                                         ▼
                              ┌─────────────────────┐
                              │      SQL Agent       │
                              │  (NL → SQL using     │
                              │   schema context)    │
                              └──────────┬───────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │    Execute Agent     │
                              │  (run SQL on         │
                              │   data.db, validate) │
                              └──────────┬───────────┘
                                         │
                              ┌──────────┴──────────┐
                              │                      │
                          Success               SQL Error
                              │                      │
                              │              ┌───────▼────────┐
                              │              │  Self-Heal Loop│
                              │              │  (retry ≤ 3x,  │
                              │              │  fix with LLM) │
                              │              └───────┬────────┘
                              │                      │
                              └──────────┬───────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │      BI Agent        │
                              │  (interpret results, │
                              │   pick chart type,   │
                              │   write narrative)   │
                              └──────────┬───────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │   AskResponse JSON   │
                              │  answer + chart_spec │
                              │  + rows_data + SQL   │
                              └──────────┬───────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │  Browser renders     │
                              │  Recharts line/bar/  │
                              │  pie/kpi + narrative │
                              └─────────────────────┘
```

---

## Project Structure

```
genbi/
├── agents/                      # AI agent modules
│   ├── bi_agent.py              # Interprets SQL results → chart spec + narrative
│   ├── dashboard_agent.py       # Proposes & executes multi-panel dashboard questions
│   ├── execute_agent.py         # Safely executes LLM-generated SELECT queries
│   ├── graph.py                 # LangGraph state machine (RAG → SQL → Execute → BI)
│   ├── llm_client.py            # Unified factory for Gemini / OpenRouter / Ollama
│   ├── metadata_agent.py        # FAISS-backed RAG over catalog.yaml
│   ├── orchestrator.py          # Per-workspace agent coordinator
│   ├── powerbi_exporter.py      # Generates .pbip zip (model.bim + report layout JSON)
│   ├── rag_agent.py             # Vector similarity search over certified reports
│   └── sql_agent.py             # Natural language → SQL with self-healing retry
│
├── api/                         # FastAPI application
│   ├── main.py                  # /ask, /dashboard/*, /export/* endpoints
│   └── workspace_routes.py      # /workspace/* lifecycle endpoints
│
├── ui/
│   └── index.html               # Single-page app (React + Recharts, no build step)
│
├── workspace/                   # Workspace management library
│   ├── csv_ingestor.py          # CSV → SQLite with chunking & type inference
│   ├── db_connector.py          # External DB connector with Fernet credential encryption
│   ├── glossary_generator.py    # LLM-based business glossary generation
│   ├── manager.py               # WorkspaceManager — lifecycle, catalog, orchestrators
│   └── schema_inferrer.py       # LLM schema profiling → catalog.yaml
│
├── watch_workspace.py           # Background watcher for ag_chat provider integration
│
├── .env.example                 # Template for required environment variables
├── requirements.txt             # Python dependencies
└── README.md
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| **AI Orchestration** | [LangChain](https://www.langchain.com/) + [LangGraph](https://langchain-ai.github.io/langgraph/) |
| **LLM Providers** | Google Gemini, OpenRouter (GPT-4o, Claude, Mistral), Ollama (local) |
| **Vector Search** | [FAISS](https://faiss.ai/) via `langchain-community` |
| **Database** | [SQLite](https://www.sqlite.org/) (per-workspace, via SQLAlchemy) |
| **Data Ingestion** | [Pandas](https://pandas.pydata.org/) + [SQLAlchemy](https://www.sqlalchemy.org/) |
| **Credential Security** | [Cryptography (Fernet)](https://cryptography.io/en/latest/fernet/) |
| **Frontend** | Vanilla HTML + [React](https://react.dev/) (CDN) + [Recharts](https://recharts.org/) (CDN) + [Tailwind CSS](https://tailwindcss.com/) (CDN) |
| **BI Export** | Custom PBIP generator (JSON-based Power BI Project format) |

---

## Setup & Installation

### Prerequisites
- Python **3.10+**
- `pip`
- (Optional) [Ollama](https://ollama.com/) for local LLM inference

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/genbi.git
cd genbi
```

### 2. Create a virtual environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in at least one LLM provider key:

```env
# Option A — Google Gemini (recommended for getting started)
GEMINI_API_KEY=your_key_here

# Option B — OpenRouter (access to GPT-4o, Claude, Mistral, etc.)
OPENROUTER_API_KEY=your_key_here

# Option C — Local Ollama (no API key needed, fully private)
OLLAMA_BASE_URL=http://localhost:11434

# Required for external DB credential encryption
FERNET_KEY=<generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
```

---

## Running the App

```bash
# Start the FastAPI server (hot-reload enabled)
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Then open your browser at **[http://127.0.0.1:8000](http://127.0.0.1:8000)**

---

## Usage Walkthrough

### Step 1 — Connect Your Data

**Option A: Upload CSV files**
- Click **"Upload CSV"** and select one or more `.csv` files
- Click **Ingest** — files are loaded into a local SQLite database

**Option B: Connect an external database**
- Click **"Connect Database"**
- Fill in host, port, credentials for PostgreSQL / MySQL / SQL Server / SQLite
- Credentials are encrypted at rest with Fernet

---

### Step 2 — Analyze Schema (AI-powered)

- Click **"Analyze Schema"**
- The LLM reads your table/column names and generates human-readable descriptions
- Review and edit the schema in the **Schema Map Editor**:
  - Edit column names (display aliases — DB unchanged)
  - Edit column types and descriptions
  - Add/remove tables and columns
- Click **Save Schema**

---

### Step 3 — Generate Business Glossary

- Click **"Generate Glossary"**
- The LLM produces 10–15 domain-specific terms mapped to table.column references
- Review, edit, or delete terms in the Glossary Editor
- Click **Save Glossary**

---

### Step 4 — Ask Questions

Type any business question in the chat box, for example:
- *"What is the monthly revenue trend in 2017?"*
- *"Which product categories generated the most sales?"*
- *"Give me a pie chart of payment types."*
- *"Show me the top 10 cities by order volume."*

GenBI will:
1. Retrieve relevant schema context via vector search
2. Generate a SQL query
3. Execute it against your database
4. Return an answer with an interactive chart

---

### Step 5 — Generate an Overview Dashboard

- Click **"Generate Dashboard"**
- GenBI auto-proposes and executes 6 analytical questions across your dataset
- Renders a multi-panel dashboard with KPI cards, line charts, bar charts, pie charts, and data tables
- The dashboard is saved and accessible from the sidebar

---

### Step 6 — Export to Power BI

- Click **"Export → Download PBIP"**
- A complete Power BI Project (`.pbip`) zip is downloaded
- Open the `.pbip` folder in **Power BI Desktop** to get a fully themed, labelled dashboard

---

## Supported LLM Providers

| Provider | How to use | Notes |
|----------|-----------|-------|
| **Google Gemini** | Set `GEMINI_API_KEY` in `.env` | Default. Fast, accurate. |
| **OpenRouter** | Set `OPENROUTER_API_KEY` in `.env` | Access to GPT-4o, Claude Sonnet, Mistral, Llama, etc. |
| **Ollama (local)** | Run `ollama pull <model>` and set `OLLAMA_BASE_URL` | Fully private, no data leaves your machine |

You can switch providers per-request from the settings dropdown in the UI.

---

## Power BI Export

GenBI generates a complete **Power BI Project (PBIP)** file structure:

```
MyDashboard.Report/
├── definition.pbir          # Report definition pointer
├── report.json              # Visual layout, page config, theme
└── model.bim                # Tabular data model (tables, columns, relationships)
```

The exported file:
- Opens directly in **Power BI Desktop** (no `.pbix` intermediate needed)
- Includes a branded header with AI-generated title and subtitle
- Applies a professional dark theme with color palette
- Contains clean panel labels derived from your schema aliases

---

## API Reference

### `POST /ask`
Submit a natural language question.

**Request body:**
```json
{
  "workspace_id": "uuid",
  "question": "What is total revenue in 2017?",
  "provider": "gemini",
  "model": "gemini-2.0-flash",
  "session_id": "uuid"
}
```

**Response:**
```json
{
  "answer": "Total revenue in 2017 was 7,250,246.73 BRL...",
  "source": "sql",
  "sql_used": "SELECT ...",
  "rows_returned": 12,
  "rows_data": [...],
  "chart_spec": {
    "chart_type": "line",
    "title": "Monthly Revenue 2017",
    "x_key": "Month",
    "y_key": "Revenue"
  }
}
```

---

### `POST /workspace/upload-csv`
Upload one or more CSV files to create a new workspace.

### `POST /workspace/connect-db`
Connect an external database.

### `POST /workspace/infer-schema`
Run LLM schema inference on the workspace.

### `POST /workspace/save-schema`
Save user-edited catalog back to disk.

### `POST /workspace/generate-glossary`
Generate a business glossary draft.

### `POST /dashboard/generate`
Generate a multi-panel executive dashboard.

### `GET /dashboard/export/{dashboard_id}?format=pbip`
Download a Power BI Project export.

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_API_KEY` | One of three | — | Google Gemini API key |
| `OPENROUTER_API_KEY` | One of three | — | OpenRouter API key |
| `OLLAMA_BASE_URL` | One of three | `http://localhost:11434` | Ollama base URL |
| `FERNET_KEY` | Yes (for external DB) | — | Fernet encryption key for DB credentials |
| `WORKSPACE_ROOT` | No | `workspaces` | Directory for workspace data storage |
| `CORS_ALLOWED_ORIGINS` | No | `*` | Allowed CORS origins |
| `MAX_SQL_ROWS` | No | `500` | Max rows returned per query |
| `MAX_CSV_SIZE_MB` | No | `100` | Max CSV upload size in MB |
| `DEFAULT_PROVIDER` | No | `gemini` | Default LLM provider |
| `DEFAULT_MODEL` | No | `gemini-2.0-flash` | Default LLM model |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
