"""
workspace/db_connector.py
--------------------------
Tests external DB connections and stores their connection strings
(Fernet-encrypted) in the workspace folder.

Supported DB types: sqlite, postgresql, mysql, mssql

FIX #4: Missing FERNET_KEY is now a hard startup error, not a warning.
        Silently generating a one-time key caused all stored .enc files to
        become permanently unreadable after a server restart.

FIX #5: Removed the plaintext fallback (connection.txt). Database credentials
        MUST be encrypted at rest. If the 'cryptography' package is not
        installed, the function raises immediately with a clear instruction
        rather than silently writing credentials in plain text.
"""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import create_engine, inspect, text


# ── Fernet encryption ─────────────────────────────────────────────────────────

def _get_fernet():
    """
    Return a Fernet instance using FERNET_KEY from the environment.

    Raises RuntimeError if:
      - The 'cryptography' package is not installed.
      - FERNET_KEY is not set in the environment.

    Generate a key once with:
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    and store it in your .env file as FERNET_KEY=<value>.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required to store external DB credentials. "
            "Install it with:  pip install cryptography"
        ) from exc

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    except ImportError:
        pass

    key = os.getenv("FERNET_KEY")
    if not key:
        raise RuntimeError(
            "FERNET_KEY is not set in the environment. "
            "External DB credentials cannot be stored without an encryption key. "
            "Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "and add FERNET_KEY=<value> to your .env file."
        )

    return Fernet(key.encode() if isinstance(key, str) else key)


def build_connection_string(
    db_type: str,
    host: str = "",
    port: str = "",
    database: str = "",
    username: str = "",
    password: str = "",
) -> str:
    """Assemble a SQLAlchemy connection URL from form fields."""
    db_type = db_type.lower()

    if db_type == "sqlite":
        return f"sqlite:///{database}"

    if db_type == "postgresql":
        driver = "postgresql+psycopg2"
    elif db_type == "mysql":
        driver = "mysql+pymysql"
    elif db_type == "mssql":
        driver = "mssql+pyodbc"
    else:
        raise ValueError(f"Unsupported DB type: {db_type}")

    port_part = f":{port}" if port else ""
    return f"{driver}://{username}:{password}@{host}{port_part}/{database}"


def test_connection(connection_string: str) -> tuple[bool, str]:
    """
    Attempt to connect using the given SQLAlchemy URL.
    Returns (success: bool, message: str).
    """
    try:
        if connection_string.startswith("sqlite:///"):
            engine = create_engine(connection_string, connect_args={"timeout": 5})
        else:
            engine = create_engine(connection_string, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Connection successful."
    except Exception as e:
        return False, str(e)


def reflect_schema(connection_string: str) -> dict:
    """
    Reflect table names and columns from an external DB.
    Returns summary in the same format as csv_ingestor.

    Raises ValueError if the database has no tables — this prevents a workspace
    where meta.json ends up with an empty 'tables' dict, which would cause
    schema inference to fail with a misleading "ingest data first" error.
    (SQLite in particular creates a blank file on first connect, so a wrong
    file path silently produces an empty DB that passes the connection test.)
    """
    engine = create_engine(connection_string)
    insp   = inspect(engine)
    tables: dict[str, dict] = {}

    for table_name in insp.get_table_names():
        cols = {c["name"]: str(c["type"]) for c in insp.get_columns(table_name)}
        tables[table_name] = {
            "source_file": table_name,
            "row_count":   None,  # avoid COUNT(*) on large remote tables
            "columns":     cols,
        }

    if not tables:
        raise ValueError(
            "No tables found in the connected database. "
            "If you are connecting a SQLite file, make sure the path points to an "
            "existing database with tables (SQLite creates a blank file for a wrong "
            "path, which passes the connection test but contains no data). "
            "For other databases, check that the user has SELECT privileges on the "
            "target schema."
        )

    return {"db_url": connection_string, "tables": tables}


def save_connection(workspace_dir: str, connection_string: str) -> str:
    """
    Encrypts and saves the connection string to
    workspaces/{workspace_id}/connection.enc.

    FIX #4/#5: No longer generates a fallback key or writes plaintext.
    Raises RuntimeError if encryption prerequisites are not met.

    Returns the file path written.
    """
    os.makedirs(workspace_dir, exist_ok=True)
    fernet = _get_fernet()  # raises if key missing or cryptography not installed

    encrypted = fernet.encrypt(connection_string.encode())
    out_path  = os.path.join(workspace_dir, "connection.enc")
    with open(out_path, "wb") as f:
        f.write(encrypted)

    return out_path


def load_connection(workspace_dir: str) -> Optional[str]:
    """
    Reads and decrypts the stored connection string for a workspace.
    Returns None if no connection.enc file exists.

    FIX #5: Removed plaintext .txt fallback. Only encrypted .enc files
    are supported. If the file exists but cannot be decrypted (wrong key),
    raises an error so the problem is visible rather than silently ignored.
    """
    enc_path = os.path.join(workspace_dir, "connection.enc")
    if not os.path.exists(enc_path):
        return None

    fernet = _get_fernet()
    with open(enc_path, "rb") as f:
        return fernet.decrypt(f.read()).decode()