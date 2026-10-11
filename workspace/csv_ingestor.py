"""
workspace/csv_ingestor.py
--------------------------
Loads one or more uploaded CSV files into a workspace-specific SQLite database.
One table per CSV file, named after the filename stem (lowercased, spaces → _).

Large-file strategy
--------------------
Files larger than CHUNK_THRESHOLD_MB are loaded in chunks of CHUNK_SIZE rows
and appended to the DB table to avoid OOM on the POC machine. A tqdm-style
progress counter is printed to the server log.

Limits (all overridable via env vars):
  MAX_CSV_SIZE_MB   — hard cap per file  (default: 500 MB)
  MAX_CSV_ROWS      — hard cap per file  (default: 5 000 000 rows)
  CHUNK_THRESHOLD_MB — above this use chunked reading (default: 80 MB)
  CHUNK_SIZE         — rows per chunk    (default: 100 000)
"""
from __future__ import annotations

import os
import re

import pandas as pd
from sqlalchemy import create_engine, inspect, text

MAX_CSV_SIZE_MB      = int(os.getenv("MAX_CSV_SIZE_MB",    "500"))
MAX_CSV_ROWS         = int(os.getenv("MAX_CSV_ROWS",       "5000000"))
CHUNK_THRESHOLD_MB   = int(os.getenv("CHUNK_THRESHOLD_MB", "80"))
CHUNK_SIZE           = int(os.getenv("CHUNK_SIZE",         "100000"))


def _safe_table_name(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    name = stem.lower().strip()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name or name[0].isdigit():
        name = "t_" + name
    return name


def _load_csv_to_db(path: str, table_name: str, engine) -> int:
    """
    Load a single CSV into `table_name`.  Uses chunked reading for large files.
    Returns the total row count loaded.
    """
    size_mb = os.path.getsize(path) / (1024 * 1024)
    total_rows = 0

    if size_mb > CHUNK_THRESHOLD_MB:
        print(f"[csv_ingestor] '{os.path.basename(path)}' is {size_mb:.0f} MB — "
              f"using chunked loading (chunk={CHUNK_SIZE:,} rows) …")

        first_chunk = True
        for chunk in pd.read_csv(path, chunksize=CHUNK_SIZE, low_memory=False):
            if total_rows + len(chunk) > MAX_CSV_ROWS:
                # Trim the last chunk to stay within limit
                remaining = MAX_CSV_ROWS - total_rows
                chunk = chunk.iloc[:remaining]
                chunk.to_sql(table_name, engine,
                             if_exists="replace" if first_chunk else "append",
                             index=False)
                total_rows += len(chunk)
                print(f"[csv_ingestor]   -> row limit {MAX_CSV_ROWS:,} reached, "
                      f"stopping at {total_rows:,} rows")
                break

            chunk.to_sql(table_name, engine,
                         if_exists="replace" if first_chunk else "append",
                         index=False)
            total_rows  += len(chunk)
            first_chunk  = False
            print(f"[csv_ingestor]   -> {total_rows:,} rows loaded ...")
    else:
        df = pd.read_csv(path, low_memory=False)
        if len(df) > MAX_CSV_ROWS:
            print(f"[csv_ingestor] Trimming '{os.path.basename(path)}' "
                  f"from {len(df):,} to {MAX_CSV_ROWS:,} rows")
            df = df.iloc[:MAX_CSV_ROWS]
        df.to_sql(table_name, engine, if_exists="replace", index=False)
        total_rows = len(df)

    return total_rows


def ingest_csvs(file_paths: list[str], db_path: str) -> dict:
    """
    Load each CSV at file_paths into db_path (SQLite).

    Returns:
        {
            "db_path": str,
            "tables": {
                table_name: {
                    "source_file": str,
                    "row_count": int,
                    "columns": { col_name: dtype_str }
                }
            }
        }
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 120},   # generous timeout for large inserts
    )

    tables: dict[str, dict] = {}

    for path in file_paths:
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > MAX_CSV_SIZE_MB:
            raise ValueError(
                f"{os.path.basename(path)} is {size_mb:.1f} MB — "
                f"exceeds the {MAX_CSV_SIZE_MB} MB limit. "
                f"Set MAX_CSV_SIZE_MB in .env to raise this."
            )

        table_name = _safe_table_name(path)
        row_count  = _load_csv_to_db(path, table_name, engine)

        # Read back dtypes from the DB for accurate reporting
        insp = inspect(engine)
        cols = {c["name"]: str(c["type"]) for c in insp.get_columns(table_name)}

        tables[table_name] = {
            "source_file": os.path.basename(path),
            "row_count":   row_count,
            "columns":     cols,
        }
        print(f"[csv_ingestor] [OK] '{os.path.basename(path)}' -> "
              f"table '{table_name}' ({row_count:,} rows, {size_mb:.1f} MB)")

    return {"db_path": db_path, "tables": tables}


def get_table_summary(db_path: str) -> dict:
    """
    Reflect an existing workspace SQLite DB and return the same summary
    format as ingest_csvs (used when re-loading an existing workspace).
    """
    engine = create_engine(f"sqlite:///{db_path}")
    insp   = inspect(engine)
    tables: dict[str, dict] = {}

    for table_name in insp.get_table_names():
        cols = {c["name"]: str(c["type"]) for c in insp.get_columns(table_name)}
        with engine.connect() as conn:
            row = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).fetchone()
            row_count = row[0] if row else 0
        tables[table_name] = {
            "source_file": table_name,
            "row_count":   row_count,
            "columns":     cols,
        }

    return {"db_path": db_path, "tables": tables}
