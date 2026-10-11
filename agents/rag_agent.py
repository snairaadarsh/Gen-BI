"""
agents/rag_agent.py
--------------------
Matches incoming questions against the certified_reports.yaml store using a
LangChain FAISS vectorstore (all-MiniLM-L6-v2 embeddings) for fast ANN
similarity search.

If similarity exceeds the threshold, the certified SQL is returned directly
— no LLM call needed.

[UPGRADE] Constructor now accepts:
  - reports_path (str): relative OR absolute path to certified_reports.yaml
  - index_dir (str | None): FAISS index location; defaults to sibling folder
Both default to original values — backward compatible.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import yaml

from agents.llm_client import get_embeddings_model

_HERE         = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

_DEFAULT_REPORTS_PATH = "metadata/certified_reports.yaml"
_DEFAULT_INDEX_DIR    = os.path.join(_PROJECT_ROOT, "metadata", "faiss_index", "certified_reports")


class RagAgent:
    def __init__(
        self,
        reports_path: str = _DEFAULT_REPORTS_PATH,
        index_dir: str | None = None,
        threshold: float = 0.88,
    ):
        # Resolve reports path — accept both relative-to-project-root and absolute
        if os.path.isabs(reports_path):
            self.abs_path = reports_path
        else:
            self.abs_path = os.path.join(_PROJECT_ROOT, reports_path)

        with open(self.abs_path) as f:
            self.reports = yaml.safe_load(f)["reports"]

        self.threshold   = threshold
        self._yaml_mtime = os.path.getmtime(self.abs_path)

        self._embeddings  = None
        self._vectorstore = None

        # FAISS index directory
        if index_dir:
            self._index_dir = index_dir
        else:
            reports_dir = os.path.dirname(self.abs_path)
            self._index_dir = os.path.join(reports_dir, "..", "faiss_index", "certified_reports")
            self._index_dir = os.path.abspath(self._index_dir)

    # ── Public API ─────────────────────────────────────────────────────────────

    def find_match(self, question: str) -> Optional[dict]:
        try:
            self._ensure_index()
            if self._vectorstore is None:
                return None  # no certified reports yet
            results = self._vectorstore.similarity_search_with_relevance_scores(question, k=1)
            if not results:
                return None

            doc, score = results[0]
            best_score = float(score)

            if best_score >= self.threshold:
                report_idx = doc.metadata["report_index"]
                match = self.reports[report_idx]
                return {**match, "similarity_score": round(best_score, 3)}

        except Exception as e:
            print(f"[RagAgent] Warning — FAISS search failed, falling through to SQL agent: {e}")

        return None

    def rebuild_index(self) -> None:
        self._build_and_save_index()

    def list_reports(self) -> list[dict]:
        return self.reports

    # ── Internal ───────────────────────────────────────────────────────────────

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

        return [
            Document(
                page_content=report["question"],
                metadata={"report_index": i, "id": report.get("id")},
            )
            for i, report in enumerate(self.reports)
        ]

    def _build_and_save_index(self):
        from langchain_community.vectorstores import FAISS

        docs = self._build_documents()

        if not docs:
            # Empty certified_reports.yaml — nothing to index yet. Skip FAISS build.
            print(f"[RagAgent] No certified reports to index — skipping FAISS build.")
            self._vectorstore = None
            return

        print(f"[RagAgent] Building FAISS index for {len(docs)} certified reports -> {self._index_dir}")
        t0 = time.time()

        self._embeddings = get_embeddings_model()
        self._vectorstore = FAISS.from_documents(docs, self._embeddings)

        os.makedirs(self._index_dir, exist_ok=True)
        self._vectorstore.save_local(self._index_dir)
        print(f"[RagAgent] FAISS index built in {time.time() - t0:.2f}s -> {self._index_dir}")

    def _load_index(self):
        from langchain_community.vectorstores import FAISS

        print(f"[RagAgent] Loading FAISS index from {self._index_dir} ...")
        self._embeddings = get_embeddings_model()
        self._vectorstore = FAISS.load_local(
            self._index_dir,
            self._embeddings,
            allow_dangerous_deserialization=True,
        )
        print(f"[RagAgent] FAISS index ready ({len(self.reports)} reports).")
