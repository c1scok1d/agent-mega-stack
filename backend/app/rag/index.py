# app/rag/index.py
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from hashlib import sha1
from typing import Dict, List, Tuple

import numpy as np

try:
    import faiss  # pip install faiss-cpu
except Exception as e:
    raise ImportError(
        "faiss not installed. Install with: python -m pip install faiss-cpu"
    ) from e

# Embeddings: sentence-transformers
from sentence_transformers import SentenceTransformer

RAG_STORE_DIR = os.getenv("RAG_STORE_DIR", ".rag_store")
EMBED_MODEL = os.getenv("EMBEDDINGS_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# ---------------------------
# Small utilities
# ---------------------------

def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _paths_for_user(user_id: str) -> Tuple[str, str, str]:
    base = os.path.join(RAG_STORE_DIR, user_id)
    _ensure_dir(base)
    return (
        base,
        os.path.join(base, "faiss.index"),
        os.path.join(base, "docstore.json"),
    )

def _hash_source(source: str) -> str:
    return sha1(source.lower().encode("utf-8")).hexdigest()[:16]

def _clean_text(s: str) -> str:
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

# ---------------------------
# Model cache
# ---------------------------

_MODEL: SentenceTransformer | None = None

def _get_model() -> SentenceTransformer:
    global _MODEL
    if _MODEL is None:
        _MODEL = SentenceTransformer(EMBED_MODEL)
    return _MODEL

def _embed_texts(texts: List[str]) -> np.ndarray:
    model = _get_model()
    vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    if vecs.ndim == 1:
        vecs = vecs.reshape(1, -1)
    return vecs.astype("float32")

# ---------------------------
# Docstore structure
# ---------------------------

@dataclass
class DocRec:
    id: str
    page_content: str
    metadata: Dict

def _load_docstore(path: str) -> Dict[str, Dict]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_docstore(path: str, data: Dict[str, Dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)

# app/rag/index.py  (append these helpers)

from collections import Counter
from typing import List, Dict, Tuple

def list_loaded_sources(user_id: str) -> List[dict]:
    """
    Return a list of sources with counts and a best-effort file_id if present.
    Example: [{"source":"resume.pdf","chunks":12,"file_id":"ab12cd34"}, ...]
    """
    vs = _load_or_build(user_id)
    counts: Counter = Counter()
    file_ids: Dict[str, str] = {}

    # FAISS docstore stores LangChain Document objects in a private dict
    for doc in vs.docstore._dict.values():
        meta = doc.metadata or {}
        src = meta.get("source", "unknown")
        counts[src] += 1
        # keep a short hash if we saved one, else derive one from the FAISS doc id
        fid = meta.get("file_id")
        if not fid:
            # fallback: derive a short id from the FAISS key if present
            # (safe if you stored keys like "<file_id>::page::<n>")
            key = getattr(doc, "page_content", "")  # not perfect, but non-crashing fallback
        if src not in file_ids:
            file_ids[src] = fid or ""

    out = []
    for src, n in sorted(counts.items()):
        out.append({"source": src, "chunks": n, "file_id": file_ids.get(src) or ""})
    return out


def rag_query_with_trace(user_id: str, query: str, *, k: int = 6) -> Tuple[List[dict], Dict[str, int]]:
    """
    Run a RAG query and also return a compact tally of which sources were used.
    Returns: (hits, counts_by_source)
      - hits: list of {"text":..., "metadata": {...}}
      - counts_by_source: {"resume.pdf": 3, "other.txt": 1, ...}
    """
    hits = rag_search(user_id, query, k=k)  # existing function returning list of dicts
    tally: Counter = Counter()
    for h in hits:
        meta = (h.get("metadata") or {})
        src = meta.get("source", "unknown")
        tally[src] += 1
    return hits, dict(tally)


# ---------------------------
# Index load/save
# ---------------------------

def _build_empty_index(d: int) -> faiss.Index:
    index = faiss.IndexFlatIP(d)  # cosine via normalized vectors
    return index

def _load_or_build(user_id: str) -> Tuple[faiss.Index, Dict[str, Dict]]:
    base, faiss_path, docstore_path = _paths_for_user(user_id)
    ds = _load_docstore(docstore_path)

    if os.path.exists(faiss_path) and os.path.getsize(faiss_path) > 0:
        index = faiss.read_index(faiss_path)
        return index, ds

    # Build empty index; dimension comes from model
    dim = int(_embed_texts(["dummy"]).shape[1])
    index = _build_empty_index(dim)
    return index, ds

def save_local(user_id: str, index: faiss.Index, docstore: Dict[str, Dict]) -> None:
    """Persist FAISS and docstore to disk (atomic where possible)."""
    base, faiss_path, docstore_path = _paths_for_user(user_id)

    # write index atomically
    with tempfile.NamedTemporaryFile(delete=False, dir=base) as tmpf:
        tmp_name = tmpf.name
    faiss.write_index(index, tmp_name)
    os.replace(tmp_name, faiss_path)

    _save_docstore(docstore_path, docstore)

# ---------------------------
# Public helpers used by API
# ---------------------------

def upsert_chunks_for_source(
    user_id: str,
    source: str,
    chunks: List[Dict],
    metadata: Dict | None = None,
) -> int:
    """
    Upsert chunk records for a given source. Each chunk dict must include "text".
    We always create new IDs (no partial update). Old chunks for the source are removed first.
    """
    source = source or "uploaded"
    metadata = dict(metadata or {})
    metadata.setdefault("source", source)

    index, docstore = _load_or_build(user_id)

    # 1) remove existing vectors for this source by rebuilding from remaining docs
    if docstore:
        remaining = {k: v for k, v in docstore.items() if v.get("metadata", {}).get("source") != source}
    else:
        remaining = {}

    # Rebuild index with "remaining"
    if remaining:
        # embed in batches
        texts = [v["page_content"] for v in remaining.values()]
        embs = _embed_texts(texts)
        new_index = _build_empty_index(embs.shape[1])
        new_index.add(embs)
        index = new_index
    else:
        # brand new
        dim = int(_embed_texts(["dummy"]).shape[1])
        index = _build_empty_index(dim)

    # 2) add new chunks
    cleaned_chunks = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        t = _clean_text(str(c.get("text", "")))
        if not t:
            continue
        md = dict(metadata)
        md.update(c.get("meta") or {})
        rec = {"page_content": t, "metadata": md}
        cleaned_chunks.append(rec)

    if not cleaned_chunks:
        # nothing to add, persist (so previous source delete sticks)
        save_local(user_id, index, remaining)
        return 0

    texts = [r["page_content"] for r in cleaned_chunks]
    vecs = _embed_texts(texts)
    index.add(vecs)

    # IDs must align in the order we add
    # We store docs in an ordered mapping via incremental UUIDs.
    for rec in cleaned_chunks:
        doc_id = str(uuid.uuid4())
        remaining[doc_id] = rec

    save_local(user_id, index, remaining)
    return len(cleaned_chunks)

def list_sources(user_id: str) -> List[Dict]:
    """
    Return a list of sources with chunk counts and a deterministic file_id.
    Example: [{"source":"resume.pdf","chunks":12,"file_id":"ab12cd34ef56..."}]
    """
    _, _, docstore_path = _paths_for_user(user_id)
    ds = _load_docstore(docstore_path)
    counts: Dict[str, int] = {}
    for v in ds.values():
        src = (v.get("metadata") or {}).get("source", "unknown")
        counts[src] = counts.get(src, 0) + 1

    out = []
    for src, n in sorted(counts.items()):
        out.append({"source": src, "chunks": n, "file_id": _hash_source(src)})
    return out

def delete_source(user_id: str, source: str) -> int:
    """
    Remove all chunks for a given source. Rebuild the FAISS index from remaining docs.
    Returns number of removed chunks.
    """
    index, docstore = _load_or_build(user_id)
    if not docstore:
        return 0

    keep = {}
    removed = 0
    for k, v in docstore.items():
        if (v.get("metadata") or {}).get("source") == source:
            removed += 1
        else:
            keep[k] = v

    if removed == 0:
        return 0

    # rebuild FAISS from keep
    if keep:
        texts = [v["page_content"] for v in keep.values()]
        embs = _embed_texts(texts)
        new_index = _build_empty_index(embs.shape[1])
        new_index.add(embs)
        index = new_index
    else:
        dim = int(_embed_texts(["dummy"]).shape[1])
        index = _build_empty_index(dim)

    save_local(user_id, index, keep)
    return removed

def rag_search(user_id: str, query: str, k: int = 5) -> List[Dict]:
    """
    Search the RAG index and return top-k snippets with metadata.
    """
    index, docstore = _load_or_build(user_id)
    if index.ntotal == 0 or not query.strip():
        return []

    qv = _embed_texts([query])
    scores, idxs = index.search(qv, min(k, max(1, index.ntotal)))
    # FAISS returns only ranks; we need to align with docstore order.
    # Our docstore is not guaranteed ordered the same way — so we reconstruct
    # the "insertion order" from current docstore iteration.
    items = list(docstore.items())  # [(id, rec), ...] order matches add()
    hits = []
    for rank, score in zip(idxs[0], scores[0]):
        if rank < 0 or rank >= len(items):
            continue
        _, rec = items[rank]
        hits.append(
            {
                "text": rec["page_content"],
                "metadata": rec.get("metadata") or {},
                "score": float(score),
            }
        )
    return hits
