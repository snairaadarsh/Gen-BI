# GenBI POC — Full Audit Implementation Plan

## Background
A full read of all 19 source files (≈3,500 lines) was completed. This plan covers every flagged issue, sorted by severity, with the exact file, line(s), and concrete fix for each one. **No new features are proposed — only hardening and cleanup of what already exists.**

---

## 🔴 CRITICAL

---

### C-1 · No Authentication on Any Route
**Files:** `api/main.py`, `api/workspace_routes.py` — All 22 endpoints  

Every route is completely open. Any caller on the network can upload CSVs, delete workspaces, query the LLM, and read all history.

**Fix:** Add a single shared API-key dependency used globally.

#### [MODIFY] api/main.py
- Add `API_KEY = os.environ.get("API_KEY")` near the top constants block  
- Add a FastAPI `Depends` function `verify_api_key(x_api_key: str = Header(...))` that raises HTTP 401 if the key doesn't match  
- Wire it as `app = FastAPI(dependencies=[Depends(verify_api_key)])` so it applies to every route automatically  
- **Skip auth on `GET /` and `GET /health`** (UI static file + heartbeat)

#### [MODIFY] .env
- Add `API_KEY=<random-uuid>` entry  
- Document it in `README` (or equivalent) so the UI sends `X-API-Key` header

---

### C-2 · No Test Suite
**Files:** None — `tests/` directory does not exist  

Zero test coverage across the entire codebase.

**Fix:** Create `tests/` with `conftest.py` and the following test modules:

| File | What it tests |
|------|--------------|
| `tests/conftest.py` | Shared fixtures: tmp workspace dir, mock LLM client, sample catalog |
| `tests/test_schema_inferrer.py` | `infer_schema()` with valid CSV, empty CSV, malformed CSV |
| `tests/test_sql_agent.py` | SQL generation given known NL + schema; self-heal retry on bad SQL |
| `tests/test_execute_agent.py` | SELECT allowed; non-SELECT rejected; row limit enforced |
| `tests/test_db_connector.py` | Fernet encrypt/decrypt round-trip; password not in error response |
| `tests/test_metadata_agent.py` | `get_context()` returns expected table/column format; alias rendering |

All LLM calls mocked via `unittest.mock.patch` — no live API calls.

---

## 🔴 HIGH

---

### H-1 · Blocking LLM Calls in `async` FastAPI Handlers (Event Loop Starvation)
**Files:** `api/main.py` L297, L566, L752; `api/workspace_routes.py` L307, L364  

Synchronous `orchestrator.handle()`, `mgr.infer_schema()`, `mgr.generate_glossary()` called directly inside `async def` handlers. Under any concurrent load the event loop is blocked for the duration of LLM calls (5–30s+).

**Fix:** Wrap each blocking call with `asyncio.to_thread()`:
```python
# Before
result = orchestrator.handle(req.question, ...)

# After
result = await asyncio.to_thread(orchestrator.handle, req.question, ...)
```
Apply same pattern to `mgr.infer_schema(...)` and `mgr.generate_glossary(...)`.

#### [MODIFY] api/main.py · api/workspace_routes.py
- Wrap all synchronous LLM/orchestrator calls in `await asyncio.to_thread(...)`
- Add `import asyncio` where missing

---

### H-2 · DB Password Leaked in HTTP Error Response
**Files:** `workspace/db_connector.py` L92; `api/workspace_routes.py` L278  

`build_connection_string()` embeds the plaintext password in the URL. If `test_connection()` throws a `SQLAlchemyError`, the exception message contains the full URL (e.g., `mysql+pymysql://user:PASSWORD@host/db`) and is returned verbatim as an HTTP 422 body.

**Fix:**
#### [MODIFY] workspace/db_connector.py
```python
def test_connection(conn_str: str) -> None:
    """Raises ValueError (without credential details) if connection fails."""
    try:
        engine = create_engine(conn_str, connect_args={"timeout": 5})
        with engine.connect():
            pass
    except Exception as exc:
        # Strip credentials from URL before surfacing
        safe = re.sub(r"://[^@]+@", "://<redacted>@", str(exc))
        raise ValueError(f"Connection failed: {safe}") from None
```
Add `import re` at top.

#### [MODIFY] api/workspace_routes.py
- The `except Exception as e: raise HTTPException(detail=str(e))` at L278 is fine once `db_connector.py` sanitises the message.

---

### H-3 · Path Traversal via Unsanitised `workspace_id`
**Files:** `api/workspace_routes.py` L50–59, L426–428; `api/main.py` L449–462  

`workspace_id` is a user-controlled query parameter used directly in `os.path.join()`. A value like `../../etc` or `../other_workspace` can escape `WORKSPACE_ROOT`.

**Fix:** Add a shared validator used at every entry point:
#### [MODIFY] workspace/manager.py
```python
import re as _re
_WS_ID_RE = _re.compile(r'^[a-zA-Z0-9_\-]{8,64}$')

def validate_workspace_id(workspace_id: str) -> str:
    """Raises ValueError if workspace_id contains path traversal or invalid chars."""
    if not _WS_ID_RE.match(workspace_id):
        raise ValueError(f"Invalid workspace_id: '{workspace_id}'")
    return workspace_id
```
#### [MODIFY] api/workspace_routes.py · api/main.py
- Call `validate_workspace_id(workspace_id)` at the top of every route handler that accepts `workspace_id`
- Raise HTTP 400 on `ValueError`

---

### H-4 · Raw User NL Input Injected Into LLM Prompts (Prompt Injection)
**Files:** `agents/sql_agent.py` L143–146; `agents/bi_agent.py` L127–133; `agents/dashboard_agent.py` L170  

The raw `question` string from the HTTP body is interpolated directly into f-string prompts with no sanitisation.

**Fix:** Add a lightweight sanitiser in a shared utility:
#### [NEW] agents/prompt_guard.py
```python
import re, logging
_MAX_QUESTION_LEN = 2000  # characters
_INJECTION_PATTERNS = [
    r"ignore (all |previous )?instructions?",
    r"disregard (the |all |previous )?(above|prior|system)",
    r"you are now",
    r"act as",
    r"new (role|persona|instructions?)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

def sanitise_question(question: str) -> str:
    """Truncate oversized input and warn on obvious injection patterns."""
    q = question.strip()
    if len(q) > _MAX_QUESTION_LEN:
        logging.warning("Question truncated from %d to %d chars", len(q), _MAX_QUESTION_LEN)
        q = q[:_MAX_QUESTION_LEN]
    if _INJECTION_RE.search(q):
        logging.warning("Possible prompt injection attempt detected in question")
    return q
```
#### [MODIFY] api/main.py
- Call `req.question = sanitise_question(req.question)` before passing to orchestrator

---

### H-5 · No SQL Execution Timeout
**File:** `agents/execute_agent.py` L58–60  

`conn.execute(text(clean))` has no timeout. A slow query hangs the handler indefinitely.

**Fix:**
#### [MODIFY] agents/execute_agent.py
```python
import signal, threading

_SQL_TIMEOUT_SEC = int(os.environ.get("SQL_TIMEOUT_SEC", "30"))

def _execute_with_timeout(engine, sql: str) -> list[dict]:
    """Run SQL and raise TimeoutError if it takes longer than _SQL_TIMEOUT_SEC."""
    result_holder: list = []
    error_holder: list = []

    def _run():
        try:
            with engine.connect() as conn:
                res = conn.execute(text(sql))
                result_holder.extend([dict(r._mapping) for r in res])
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=_SQL_TIMEOUT_SEC)
    if t.is_alive():
        raise TimeoutError(f"SQL query exceeded {_SQL_TIMEOUT_SEC}s timeout")
    if error_holder:
        raise error_holder[0]
    return result_holder
```
Replace the direct `conn.execute()` block in `ExecuteAgent.run()` with `_execute_with_timeout(self.engine, clean)`.

---

### H-6 · No Validation That LLM SQL Uses Real Table/Column Names
**Files:** `agents/execute_agent.py` L52–65; `agents/graph.py` L196  

LLM-generated SQL is never checked against the workspace schema before execution. A hallucinated table name will only fail at DB execution time (reactive), not before.

**Fix:** Add a proactive allowlist check using `sqlglot`:
#### [MODIFY] agents/execute_agent.py
```python
import sqlglot

def validate_sql_against_schema(sql: str, allowed_tables: set[str]) -> None:
    """
    Parse the SQL with sqlglot and verify every referenced table exists
    in allowed_tables. Raises ValueError if any unknown table is found.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect="sqlite")
        used_tables = {
            t.name.lower()
            for t in parsed.find_all(sqlglot.exp.Table)
        }
        unknown = used_tables - {t.lower() for t in allowed_tables}
        if unknown:
            raise ValueError(f"SQL references unknown table(s): {unknown}")
    except sqlglot.errors.ParseError:
        pass  # let DB execution surface the parse error
```
- Add `sqlglot>=23.0.0` to `requirements.txt`
- Call `validate_sql_against_schema(clean, self.allowed_tables)` in `ExecuteAgent.run()` before execution
- Pass `allowed_tables` into `ExecuteAgent.__init__()` from `GenBIGraph` (which already has access to `metadata_agent.get_all_tables()`)

---

### H-7 · Missing `catalog.yaml` / `certified_reports.yaml` Raises Unhandled 500
**Files:** `agents/metadata_agent.py` L57; `agents/rag_agent.py` L46–47  

If these files are missing or empty, `__init__()` raises `FileNotFoundError` or `TypeError` which propagates as a 500 with no helpful message.

**Fix:**
#### [MODIFY] agents/metadata_agent.py
```python
# In __init__ where catalog.yaml is loaded:
if not os.path.exists(self.abs_path):
    raise FileNotFoundError(
        f"Catalog not found at '{self.abs_path}'. "
        "Run schema inference first."
    )
```
Catch this in `WorkspaceManager` and raise a clean `ValueError` that becomes HTTP 400.

#### [MODIFY] agents/rag_agent.py
```python
if not os.path.exists(self.abs_path):
    self._reports = []
    return  # graceful degradation — RAG simply returns nothing
data = yaml.safe_load(f) or {}
self._reports = data.get("reports", [])
```

---

## 🟡 MEDIUM

---

### M-1 · `strategy` Parameter in `DashboardAgent.generate_general()` Is Ignored
**File:** `agents/dashboard_agent.py` L91–112  

Parameter is accepted in the signature but always ignored — `_llm_propose_questions()` is always called regardless.

**Fix:** Either remove the `strategy` parameter entirely, or wire it so `strategy="schema"` uses a schema-only approach. Since only `"llm"` is implemented, **remove the param** and update callers and API docs accordingly.

#### [MODIFY] agents/dashboard_agent.py · api/main.py

---

### M-2 · No Token/Size Limit on LLM Context
**Files:** `agents/bi_agent.py` L124; `agents/metadata_agent.py` L74–107  

Up to 500 rows of result JSON and unlimited catalog size are passed into LLM prompts with no size guard.

**Fix:**
#### [MODIFY] agents/bi_agent.py
```python
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "60000"))
result_json_str = json.dumps(sample, ensure_ascii=False)
if len(result_json_str) > MAX_PROMPT_CHARS:
    logging.warning("BI prompt result truncated: %d chars", len(result_json_str))
    result_json_str = result_json_str[:MAX_PROMPT_CHARS] + "\n... [truncated]"
```
#### [MODIFY] agents/metadata_agent.py
```python
context = "\n".join(lines)
if len(context) > MAX_PROMPT_CHARS:
    logging.warning("Metadata context exceeds %d chars — schema may be truncated", MAX_PROMPT_CHARS)
return context
```

---

### M-3 · Silent Swallowed Exceptions (Bare `except` + `pass`)
**File:** `api/main.py` L339, L420, L431, L605, L631, L669; `agents/orchestrator.py` L103–108  

Exceptions are silently discarded with no log, making debugging impossible.

**Fix:** Replace every `except Exception: pass` with at minimum:
```python
except Exception:
    logging.exception("Unexpected error in <context>")
```
Keep the fallback behaviour (return empty list, etc.) but always log.

---

### M-4 · No Input Validation on `format` Param in `/dashboard/export`
**File:** `api/main.py` L679  

Any string is accepted as `format`. Only `"pbip"` is implemented.

**Fix:**
```python
from typing import Literal
@app.get("/dashboard/export/{dashboard_id}")
def dashboard_export(workspace_id: str, dashboard_id: str, format: Literal["pbip"] = "pbip"):
```

---

### M-5 · No File Size Limit on CSV Upload; No Filename Sanitisation
**File:** `api/workspace_routes.py` L226–229  

`await upload.read()` reads entire file into RAM. `upload.filename` is used directly in `os.path.join()`.

**Fix:**
#### [MODIFY] api/workspace_routes.py
```python
import pathlib, re as _re

_SAFE_FNAME = _re.compile(r'^[\w\-. ]+$')
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_CSV_SIZE_MB", "100")) * 1024 * 1024

raw = await upload.read(MAX_UPLOAD_BYTES + 1)
if len(raw) > MAX_UPLOAD_BYTES:
    raise HTTPException(413, "File exceeds maximum allowed size")

fname = pathlib.Path(upload.filename).name  # strip any directory component
if not _SAFE_FNAME.match(fname):
    raise HTTPException(400, "Invalid filename")
```

---

### M-6 · `ConnectDBRequest.db_type` Accepts Any String
**File:** `api/workspace_routes.py` L88–94  

**Fix:**
```python
from typing import Literal
class ConnectDBRequest(BaseModel):
    db_type: Literal["sqlite", "postgresql", "mysql", "mssql"]
    ...
```

---

### M-7 · Dead Code — `get_sql_execution_tool()` Never Used
**File:** `agents/execute_agent.py` L103–107  

Factory function defined but never imported or called anywhere.

**Fix:** Delete lines 103–107.

---

### M-8 · Hardcoded Olist Demo Table Heuristics in `MetadataAgent`
**File:** `agents/metadata_agent.py` L141–148  

Dataset-specific logic (`"orders"`, `"order_items"`, `"customers"`) baked into generic retrieval code. Will produce wrong results on any other dataset.

**Fix:** Delete lines 141–148. The join information is already expressed in `catalog.yaml` under `joins:` per table — the metadata agent already includes those in context at L95. The heuristics are redundant and harmful for generic use.

---

## 🟢 LOW

---

### L-1 · `import uuid as _uuid` Is Unused in `main.py`
**File:** `api/main.py` L438  
**Fix:** Remove the import.

---

### L-2 · `_fallback_catalog()` and `_fallback_glossary()` Are Dead Code
**Files:** `workspace/schema_inferrer.py` L127–141; `workspace/glossary_generator.py` L104–113  
**Fix:** Delete both dead functions.

---

### L-3 · Duplicate `_call_llm()` in `schema_inferrer.py` and `glossary_generator.py`
**Severity:** Low — code duplication  
**Fix:** Extract to `workspace/_llm_utils.py` and import from both modules. Single function to maintain.

---

### L-4 · `timeout_seconds = 200` Hardcoded Three Times; Wrong Error Message
**File:** `api/main.py` L227, L519, L752  

The error message at L258 says `"timed out after 120s"` while the actual timeout is 200s.

**Fix:**
```python
# Near top of main.py constants section
_AG_CHAT_TIMEOUT_SEC = int(os.environ.get("AG_CHAT_TIMEOUT_SEC", "200"))
```
Replace all three `timeout_seconds = 200` occurrences with `_AG_CHAT_TIMEOUT_SEC`, and fix the error message at L258.

---

### L-5 · `"http://localhost:8000"` Hardcoded in Export Handler
**File:** `api/main.py` L710  
**Fix:** Read from env: `API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")`

---

### L-6 · `result_rows[:500]` in `bi_agent.py` Should Use Shared Constant
**File:** `agents/bi_agent.py` L124  
**Fix:** Import `MAX_ROWS` from `execute_agent` or a shared `constants.py` and use it here.

---

### L-7 · `mgr._meta` Accessed Directly From `workspace_routes.py`
**File:** `api/workspace_routes.py` L285, L316, L330, L411  

Private attribute accessed from outside the class.

**Fix:** Add a `WorkspaceManager.meta` property exposing only what routes need.

---

### L-8 · Missing `pyodbc` in `requirements.txt`
**File:** `requirements.txt`  

`db_connector.py` uses `mssql+pyodbc` driver but `pyodbc` is not listed.

**Fix:** Add `pyodbc>=4.0.0` to `requirements.txt`.

---

### L-9 · Missing Docstrings on 18 Functions/Methods
**Fix:** Add one-line docstrings to all functions listed in the audit report.  
Priority files: `execute_agent.py`, `graph.py`, `metadata_agent.py`, `dashboard_agent.py`.

---

## Proposed Execution Order

> [!IMPORTANT]
> Issues are grouped into phases so each phase is independently mergeable and testable.

### Phase 1 — Security Hardening (C-1, H-2, H-3, H-4, H-5, H-6, H-7)
Critical security fixes first. These change the minimum viable surface area of the API.

### Phase 2 — Async Fix (H-1)
Wrap all blocking LLM calls with `asyncio.to_thread`. Requires regression-testing the full ask→sql→execute→interpret pipeline.

### Phase 3 — Input Validation (M-4, M-5, M-6)
Tighten all Pydantic models and request parameter validation.

### Phase 4 — Error Handling (M-3, H-7)
Replace all silent `except: pass` with logged fallbacks. Fix the 500-error paths.

### Phase 5 — Dead Code Cleanup (M-7, M-8, L-1 through L-6)
Remove dead functions, unused imports, hardcoded heuristics, duplicate `_call_llm`.

### Phase 6 — Tests (C-2)
Write the test suite. Tests are easiest to write after the codebase is hardened because the interfaces are stable.

### Phase 7 — Docstrings & Naming (L-9, L-7)
Cosmetic but important for long-term maintainability.

---

## Verification Plan

### Automated (after Phase 6)
```bash
pytest tests/ -v --tb=short         # all unit tests
pytest tests/ -v --cov=agents --cov=workspace --cov-report=term-missing
```

### Manual
- Upload a CSV → infer schema → ask a question → confirm SQL executes and result appears
- Try `workspace_id=../../etc` → confirm HTTP 400
- Try uploading a >100MB file → confirm HTTP 413
- Try a prompt injection question → confirm it's truncated/flagged in logs
- Remove `catalog.yaml` → ask a question → confirm HTTP 400 (not 500)
- Confirm LLM API key not logged anywhere in uvicorn output
