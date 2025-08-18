# app/api/search.py
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import Authed, get_current_user
from app.rag.index import rag_search, list_loaded_sources, rag_query_with_trace

router = APIRouter()

class SearchIn(BaseModel):
    query: str = Field(..., min_length=1)
    k: Optional[int] = 5

@router.post("/v1/search")
def search_docs(payload: SearchIn, user: Authed = Depends(get_current_user)):
    try:
        hits = rag_search(user.user_id, payload.query, k=payload.k or 5)
        return {"query": payload.query, "k": payload.k or 5, "hits": hits}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e!s}")


@router.get("/v1/rag/sources")
def rag_sources(user: Authed = Depends(get_current_user)):
    """
    List the currently indexed files/sources for this user with chunk counts.
    """
    try:
        items = list_loaded_sources(user.user_id)
        total = sum(x["chunks"] for x in items)
        return {"sources": items, "total_chunks": total}
    except Exception as e:
        raise HTTPException(500, f"List failed: {e!s}")


class RAGQueryIn(BaseModel):
    query: str
    k: int = 6

@router.post("/v1/rag/query")
def rag_query(body: RAGQueryIn, user: Authed = Depends(get_current_user)):
    """
    Run a traced RAG query and see which files contributed chunks.
    """
    try:
        hits, tally = rag_query_with_trace(user.user_id, body.query, k=body.k)
        # trim hit texts a bit for readability
        out_hits = []
        for h in hits:
            txt = (h.get("text") or "").strip()
            meta = h.get("metadata") or {}
            out_hits.append({
                "source": meta.get("source", "unknown"),
                "file_id": meta.get("file_id", ""),
                "text": txt[:400] + ("…" if len(txt) > 400 else "")
            })
        return {"hits": out_hits, "by_source": tally}
    except Exception as e:
        raise HTTPException(500, f"Query failed: {e!s}")