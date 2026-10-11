"""
agents/metadata_agent.py
-------------------------
Loads catalog.yaml and serves a compact, relevant context slice to the SQL
agent — NOT the full catalog every time.

Matching strategy: LangChain FAISS vector retriever over per-table /
per-glossary-term documents, embedded with the same all-MiniLM-L6-v2 model
used elsewhere in the system.

Index lifecycle
----------------
- On first load (or when catalog.yaml is newer than the saved index), the
  index is built from one Document per table and one Document per glossary
  term, then persisted to <index_dir>.
- On subsequent loads, the saved index is read from disk.
- Call  metadata_agent.rebuild_index()  to force a full rebuild.

[UPGRADE] Constructor now accepts:
  - catalog_path (str): relative OR absolute path to catalog.yaml
  - index_dir (str | None): where to store the FAISS index; defaults to
    a sibling 'faiss_index/catalog' folder next to catalog.yaml
Both default to their original values so existing code is unchanged.
"""
from __future__ import annotations

import os
import time

import yaml

from agents.llm_client import get_embeddings_model

_HERE         = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

# Default catalog path (relative to project root — matches original behaviour)
_DEFAULT_CATALOG_PATH = "metadata/catalog.yaml"
_DEFAULT_INDEX_DIR    = os.path.join(_PROJECT_ROOT, "metadata", "faiss_index", "catalog")

# Number of top-matching documents to retrieve per query
_TOP_K = 5


class MetadataAgent:
    def __init__(
        self,
        catalog_path: str = _DEFAULT_CATALOG_PATH,
        index_dir: str | None = None,
    ):
        # Resolve catalog path — accept both relative-to-project-root and absolute
        if os.path.isabs(catalog_path):
            self.abs_path = catalog_path
        else:
            self.abs_path = os.path.join(_PROJECT_ROOT, catalog_path)

        with open(self.abs_path) as f:
            self.catalog = yaml.safe_load(f)

        self._yaml_mtime = os.path.getmtime(self.abs_path)
        self._embeddings = None
        self._vectorstore = None

        # FAISS index directory: caller-supplied, or sibling of the catalog file
        if index_dir:
            self._index_dir = index_dir
        else:
            catalog_dir = os.path.dirname(self.abs_path)
            self._index_dir = os.path.join(catalog_dir, "..", "faiss_index", "catalog")
            self._index_dir = os.path.abspath(self._index_dir)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_context(self, question: str) -> str:
        all_tables = self.get_all_tables()
        if len(all_tables) <= 15:
            relevant_tables = all_tables
            # Still retrieve only relevant glossary hits
            _, glossary_hits = self._retrieve(question)
        else:
            relevant_tables, glossary_hits = self._retrieve(question)

        lines = []
        lines.append("Database: SQLite (use SQLite-compatible syntax ONLY: strftime(), date(), etc.)")
        lines.append("")
        # Explicitly list all available tables so the LLM never guesses
        lines.append(f"AVAILABLE TABLES (use ONLY these exact names): {', '.join(all_tables)}")
        lines.append("")

        for t in relevant_tables:
            info = self.catalog["tables"][t]
            aliases = info.get("aliases", {})
            lines.append(f"Table {t}: {info['description']}")
            for col, desc in info["columns"].items():
                alias = aliases.get(col, "")
                alias_part = f" (display name: '{alias}')" if alias and alias != col else ""
                lines.append(f"  - {col}{alias_part}: {desc}")
            for j in info.get("joins", []):
                lines.append(f"  join: {j}")
            lines.append("")

        if glossary_hits:
            lines.append("--- Glossary ---")
            for term, definition in glossary_hits:
                lines.append(f"  {term}: {definition}")

        return "\n".join(lines)


    def get_all_tables(self) -> list[str]:
        return list(self.catalog["tables"].keys())

    def rebuild_index(self) -> None:
        self._build_and_save_index()

    # ── Internal: retrieval ───────────────────────────────────────────────────

    def _retrieve(self, question: str) -> tuple[list[str], list[tuple[str, str]]]:
        try:
            self._ensure_index()
            docs = self._vectorstore.similarity_search(question, k=_TOP_K)

            matched_tables: list[str] = []
            glossary_hits: list[tuple[str, str]] = []

            for doc in docs:
                kind = doc.metadata.get("kind")
                if kind == "table":
                    name = doc.metadata["name"]
                    if name not in matched_tables:
                        matched_tables.append(name)
                elif kind == "glossary":
                    term = doc.metadata["term"]
                    definition = self.catalog["glossary"][term]
                    glossary_hits.append((term, definition))

            if not matched_tables:
                matched_tables = list(self.catalog["tables"].keys())

            # Cross-table join heuristics (preserve original logic)
            if "orders" in matched_tables and "order_items" not in matched_tables:
                matched_tables.append("order_items")
            if "customers" in matched_tables and "orders" not in matched_tables:
                matched_tables.append("orders")
            if "sellers" in matched_tables and "order_items" not in matched_tables:
                matched_tables.append("order_items")
            if "products" in matched_tables and "order_items" not in matched_tables:
                matched_tables.append("order_items")

            return matched_tables, glossary_hits

        except Exception as e:
            print(f"[MetadataAgent] Warning — FAISS retrieval failed, falling back to full catalog: {e}")
            return list(self.catalog["tables"].keys()), []

    # ── Internal: index lifecycle ─────────────────────────────────────────────

    def _ensure_index(self):
        if self._vectorstore is not None:
            return

        if self._index_is_fresh():
            self._load_index()
        else:
            self._build_and_save_index()

    def _index_is_fresh(self) -> bool:
        index_file = os.path.join(self._index_dir, "index.faiss")
        if not os.path.exists(index_file):
            return False
        return os.path.getmtime(index_file) > self._yaml_mtime

    def _build_documents(self):
        from langchain_core.documents import Document

        docs = []
        for name, info in self.catalog["tables"].items():
            col_text = "; ".join(f"{c}: {d}" for c, d in info["columns"].items())
            text = f"Table {name}: {info['description']} Columns: {col_text}"
            docs.append(Document(page_content=text, metadata={"kind": "table", "name": name}))

        for term, definition in self.catalog.get("glossary", {}).items():
            text = f"{term}: {definition}"
            docs.append(Document(page_content=text, metadata={"kind": "glossary", "term": term}))

        return docs

    def _build_and_save_index(self):
        from langchain_community.vectorstores import FAISS

        docs = self._build_documents()

        if not docs:
            print(f"[MetadataAgent] No catalog documents to index — skipping FAISS build.")
            self._vectorstore = None
            return

        print(f"[MetadataAgent] Building FAISS catalog index -> {self._index_dir}")
        t0 = time.time()

        self._embeddings = get_embeddings_model()
        self._vectorstore = FAISS.from_documents(docs, self._embeddings)

        os.makedirs(self._index_dir, exist_ok=True)
        self._vectorstore.save_local(self._index_dir)
        print(f"[MetadataAgent] FAISS catalog index built in {time.time() - t0:.2f}s "
              f"({len(docs)} documents) -> {self._index_dir}")

    def _load_index(self):
        from langchain_community.vectorstores import FAISS

        print(f"[MetadataAgent] Loading FAISS catalog index from {self._index_dir} ...")
        self._embeddings = get_embeddings_model()
        self._vectorstore = FAISS.load_local(
            self._index_dir,
            self._embeddings,
            allow_dangerous_deserialization=True,
        )
        print("[MetadataAgent] FAISS catalog index ready.")
