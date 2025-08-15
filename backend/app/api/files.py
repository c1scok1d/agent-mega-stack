# app/api/files.py
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from typing import List

from app.core.security import Authed, get_current_user
from app.rag.index import (
    upsert_text,
    similarity_search_with_scores,
    list_sources,
    delete_source,
)

router = APIRouter()


class SearchIn(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(4, ge=1, le=20)


@router.post("/v1/files")
async def upload_file(
    file: UploadFile = File(...),
    user: Authed = Depends(get_current_user),
):
    try:
        raw = await file.read()
        # try strict utf-8 first; fallback to ignoring errors for odd encodings
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="ignore")

        if not text.strip():
            raise HTTPException(status_code=400, detail="Empty file")

        chunks_added = upsert_text(user.user_id, text, source=file.filename or "upload")
        return {
            "ok": True,
            "filename": file.filename,
            "size_bytes": len(raw),
            "chunks_indexed": chunks_added,
            "embedding_backend": "fastembed",  # or whatever you set in index.py
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e!s}")


@router.get("/v1/files")
def list_files(user: Authed = Depends(get_current_user)):
    """
    List distinct sources (filenames) and how many chunks each has.
    """
    try:
        items = list_sources(user.user_id)
        total_chunks = sum(i["chunks"] for i in items)
        return {"files": items, "total_chunks": total_chunks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"List failed: {e!s}")


@router.delete("/v1/files/{source}")
def delete_file(
    source: str = Path(..., description="Exact source/filename used on upload"),
    user: Authed = Depends(get_current_user),
):
    """
    Delete all chunks that came from the given source (filename).
    """
    try:
        removed = delete_source(user.user_id, source)
        if removed == 0:
            # Not an error; just nothing to remove
            return {"ok": True, "removed": 0, "message": "No chunks matched this source."}
        return {"ok": True, "removed": removed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e!s}")


@router.post("/v1/files/search")
def search_files(body: SearchIn, user: Authed = Depends(get_current_user)):
    """
    Semantic search over the user's indexed chunks.
    """
    try:
        results = similarity_search_with_scores(user.user_id, body.query, k=body.k)
        # Normalize shape for the API
        out = []
        for d, score in results:
            out.append(
                {
                    "text": d.page_content,
                    "source": (d.metadata or {}).get("source"),
                    "score": float(score),
                }
            )
        return {"query": body.query, "results": out}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e!s}") 
