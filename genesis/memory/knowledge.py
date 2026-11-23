"""Local knowledge base: ingest → chunk → embed → store → retrieve (RAG).

Documents stay on the user's machine; vectors are stored as BLOBs in SQLite
and searched with cosine similarity (brute force is fast for personal-scale
collections of a few thousand chunks).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..core.logging_setup import get_logger
from ..database.db import Database, utcnow
from .embeddings import EmbeddingProvider

log = get_logger("knowledge")

TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".html", ".css", ".json", ".csv",
             ".yaml", ".yml", ".toml", ".ini", ".log", ".rst", ".tex", ".sql"}
SUPPORTED_EXTS = TEXT_EXTS | {".pdf", ".docx"}
MAX_FILE_BYTES = 30 * 1024 * 1024


class KnowledgeError(RuntimeError):
    pass


def read_document(path: Path) -> str:
    ext = path.suffix.lower()
    if path.stat().st_size > MAX_FILE_BYTES:
        raise KnowledgeError(f"{path.name} is larger than 30 MB.")
    if ext in TEXT_EXTS:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise KnowledgeError("Reading PDFs needs the 'pypdf' package.") from exc
        reader = PdfReader(str(path))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    if ext == ".docx":
        try:
            import docx  # type: ignore
        except ImportError as exc:
            raise KnowledgeError("Reading .docx files needs the 'python-docx' package.") from exc
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    raise KnowledgeError(f"Unsupported file type: {ext or 'no extension'}")


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text or "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
            continue
        if cur:
            chunks.append(cur)
        while len(p) > size:
            cut = p.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            chunks.append(p[:cut].strip())
            p = p[max(0, cut - overlap):].strip()
        cur = p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) > 20]


class KnowledgeBase:
    def __init__(self, db: Database, embedder_factory: Callable[[], EmbeddingProvider]) -> None:
        self.db = db
        self._factory = embedder_factory
        self._embedder: Optional[EmbeddingProvider] = None

    @property
    def embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = self._factory()
        return self._embedder

    def ingest(self, path: Path) -> dict:
        path = Path(path)
        if path.is_dir():
            results = [self.ingest(p) for p in sorted(path.rglob("*")) if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
            return {"documents": len(results), "chunks": sum(r["chunks"] for r in results), "path": str(path)}
        if path.suffix.lower() not in SUPPORTED_EXTS:
            raise KnowledgeError(f"Unsupported file type: {path.suffix or 'none'}")
        text = read_document(path)
        sha = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        existing = self.db.query_one("SELECT id, chunks FROM knowledge_documents WHERE sha256=?", (sha,))
        if existing:
            return {"documents": 1, "chunks": existing["chunks"], "path": str(path), "already_indexed": True}
        chunks = chunk_text(text)
        if not chunks:
            raise KnowledgeError(f"No readable text found in {path.name}.")
        vectors = self.embedder.embed(chunks)
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO knowledge_documents(path,title,sha256,chunks,created_at) VALUES(?,?,?,?,?)",
                (str(path), path.name, sha, len(chunks), utcnow()),
            )
            doc_id = cur.lastrowid
            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                conn.execute(
                    "INSERT INTO knowledge_chunks(document_id,idx,text,embedding,embedder) VALUES(?,?,?,?,?)",
                    (doc_id, i, chunk, np.asarray(vec, dtype=np.float32).tobytes(), self.embedder.name),
                )
        log.info("indexed %s (%d chunks)", path.name, len(chunks))
        return {"documents": 1, "chunks": len(chunks), "path": str(path)}

    def documents(self) -> list[dict]:
        return self.db.query("SELECT id,path,title,chunks,created_at FROM knowledge_documents ORDER BY id DESC")

    def remove(self, doc_id: int) -> bool:
        return self.db.execute("DELETE FROM knowledge_documents WHERE id=?", (doc_id,)).rowcount > 0

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM knowledge_chunks")
        return int(row["n"]) if row else 0

    def search(self, query: str, k: int = 4, min_score: float = 0.05) -> list[dict]:
        rows = self.db.query(
            "SELECT c.id, c.text, c.embedding, c.embedder, d.title, d.path FROM knowledge_chunks c "
            "JOIN knowledge_documents d ON d.id=c.document_id"
        )
        if not rows:
            return []
        name = self.embedder.name
        stale = [r for r in rows if r["embedder"] != name]
        if stale:  # embedder changed since indexing — re-embed transparently
            vecs = self.embedder.embed([r["text"] for r in stale])
            for r, v in zip(stale, vecs):
                blob = np.asarray(v, dtype=np.float32).tobytes()
                self.db.execute("UPDATE knowledge_chunks SET embedding=?, embedder=? WHERE id=?", (blob, name, r["id"]))
                r["embedding"], r["embedder"] = blob, name
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        q = self.embedder.embed([query])[0]
        scores = matrix @ q
        order = np.argsort(-scores)[:k]
        return [{"text": rows[i]["text"], "title": rows[i]["title"], "path": rows[i]["path"], "score": float(scores[i])}
                for i in order if scores[i] >= min_score]
