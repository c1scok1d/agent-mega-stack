# backend/app/rag/index.py
from __future__ import annotations

import os
import re
import pathlib
from typing import List, Tuple, Optional

from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.docstore.document import Document
from openai import BadRequestError  # used only to catch llama.cpp-style 400s in case you switch back
from app.core.settings import settings

# ---------- storage root ----------
DATA_DIR = pathlib.Path(getattr(settings, "RAG_DIR", "./.rag"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- embeddings (FastEmbed – no Torch required) ----------
_EMBED_MODEL = os.getenv("EMBEDDINGS_MODEL", "BAAI/bge-small-en-v1.5")

def _embeddings() -> FastEmbedEmbeddings:
    # Fast, CPU-friendly, no PyTorch dependency
    return FastEmbedEmbeddings(model_name=_EMBED_MODEL)

print(f"[RAG] embeddings backend=fastembed model={_EMBED_MODEL}")

# ---------- cleaning for RAG ----------
_CTRL_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")
_IM_TAG_RE = re.compile(r"<\|im_(start|end)\|>")

def clean_text_for_rag(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL_RE.sub(" ", text)
    text = _IM_TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ---------- paths ----------
def _user_dir(user_id: str) -> str:
    d = DATA_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return str(d)

# ---------- vector store helpers ----------
def _new_empty_store() -> FAISS:
    """
    Create an empty FAISS index bound to our embedding function.
    We bootstrap with a tiny dummy then wipe it to construct the index.
    """
    emb = _embeddings()
    ds = FAISS.from_texts(["__init__"], embedding=emb)
    ds.index.reset()
    ds.docstore._dict.clear()
    ds.index_to_docstore_id.clear()
    return ds

def _load_or_build(user_id: str) -> FAISS:
    emb = _embeddings()
    path = _user_dir(user_id)
    # Try to load an existing index
    if any(pathlib.Path(path).glob("index*")):
        try:
            return FAISS.load_local(
                path, embeddings=emb, allow_dangerous_deserialization=True
            )
        except Exception:
            # corrupted/incompatible – rebuild
            pass
    # Build empty
    return _new_empty_store()

# ---------- public APIs ----------
def upsert_text(user_id: str, text: str, source: str = "upload") -> None:
    text = clean_text_for_rag(text)
    if not text:
        return

    # small, safe chunks
    chunks = _split_for_embeddings(text, max_chars=1200, overlap=100)
    if not chunks:
        return

    vs = _load_or_build(user_id)

    # embed per-chunk; skip any bad ones
    docs = [Document(page_content=c, metadata={"source": source}) for c in chunks]
    added, skipped = _safe_embed_add(vs, docs)

    # persist only if we actually added vectors
    if added:
        vs.save_local(_user_dir(user_id))

    if skipped and not added:
        # All chunks failed → surface a clear error
        raise RuntimeError(
            "All chunks failed to embed. Remove special tags or ensure embeddings backend is FastEmbed."
        )

def similarity_search(user_id: str, query: str, k: int = 4) -> List[Document]:
    query = clean_text_for_rag(query)
    vs = _load_or_build(user_id)
    return vs.similarity_search(query, k=k)

def similarity_search_with_scores(user_id: str, query: str, k: int = 4) -> List[Tuple[Document, float]]:
    query = clean_text_for_rag(query)
    vs = _load_or_build(user_id)
    return vs.similarity_search_with_score(query, k=k)

# ---------- back-compat wrappers (used by tools.py) ----------
def rag_search(user_id: str, query: str, k: int = 4):
    docs = similarity_search(user_id, query, k=k)
    return [{"text": d.page_content, "metadata": dict(d.metadata or {})} for d in docs]

def rag_search_with_scores(user_id: str, query: str, k: int = 4):
    results = similarity_search_with_scores(user_id, query, k=k)
    out = []
    for d, score in results:
        out.append({"text": d.page_content, "metadata": dict(d.metadata or {}), "score": float(score)})
    return out

# ---------- chunking & safe add ----------
def _split_for_embeddings(text: str, max_chars: int = 1200, overlap: int = 100) -> list[str]:
    """
    Char-based chunking for embeddings.
    Splits on paragraph/line boundaries where possible; falls back to hard split.
    """
    text = text.strip()
    if not text:
        return []

    paras = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush():
        nonlocal cur, cur_len
        if cur_len:
            chunks.append("\n".join(cur).strip())
        cur, cur_len = [], 0

    for p in paras:
        if len(p) > max_chars:
            lines = p.splitlines()
            buf: list[str] = []
            buf_len = 0
            for line in lines:
                if buf_len + len(line) + 1 <= max_chars:
                    buf.append(line); buf_len += len(line) + 1
                else:
                    if buf:
                        chunks.append("\n".join(buf).strip())
                    tail = "\n".join(buf)[-overlap:] if buf else ""
                    buf = [tail, line] if tail else [line]
                    buf_len = len(tail) + len(line) + (1 if tail else 0)
            if buf:
                chunks.append("\n".join(buf).strip())
        else:
            if cur_len + len(p) + 2 <= max_chars:
                cur.append(p); cur_len += len(p) + 2
            else:
                flush()
                if chunks:
                    tail = chunks[-1][-overlap:]
                    cur = [tail, p] if tail else [p]
                    cur_len = len(tail) + len(p) + (1 if tail else 0)
                else:
                    cur = [p]; cur_len = len(p)
    flush()

    # drop empties + dedupe
    out = [c for c in (s.strip() for s in chunks) if c]
    uniq: list[str] = []
    seen: set[str] = set()
    for c in out:
        if c not in seen:
            uniq.append(c); seen.add(c)
    return uniq

def _safe_embed_add(vs: FAISS, docs: list[Document]) -> tuple[int, int]:
    """
    Embeds docs one-by-one so a single bad chunk doesn't fail the whole request.
    Returns (added_count, skipped_count).
    """
    added = skipped = 0
    for d in docs:
        try:
            vs.add_documents([d])
            added += 1
        except BadRequestError:
            # kept for compatibility if someone toggles back to an HTTP embedding endpoint
            skipped += 1
        except Exception:
            skipped += 1
    return added, skipped


def list_sources(user_id: str) -> list[dict]:
    """
    Return a list of sources with their chunk counts.
    Example: [{"source":"file1.txt","chunks":12}, ...]
    """
    vs = _load_or_build(user_id)
    counts: dict[str, int] = {}
    # iterate docs from FAISS docstore
    for doc in vs.docstore._dict.values():
        src = (doc.metadata or {}).get("source", "unknown")
        counts[src] = counts.get(src, 0) + 1
    return [{"source": s, "chunks": n} for s, n in sorted(counts.items())]
    


def delete_source(user_id: str, source: str) -> int:
    """
    Delete all chunks whose metadata.source == `source`.
    Returns number of chunks removed.
    """
    vs = _load_or_build(user_id)

    # Find matching doc IDs
    to_delete: list[str] = []
    for doc_id, doc in list(vs.docstore._dict.items()):
        if (doc.metadata or {}).get("source") == source:
            to_delete.append(doc_id)

    if not to_delete:
        return 0

    # Remove from vectorstore + docstore
    vs.delete(to_delete)
    vs.save_local(_user_dir(user_id))
    return len(to_delete)
