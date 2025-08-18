# app/api/files.py
from __future__ import annotations

import os
import shutil
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Path

from app.api.auth import Authed, get_current_user
from app.core.ingest import extract_text_from_any, normalize_text, smart_chunk, is_file_in_rag
from app.rag.index import (
    upsert_chunks_for_source,
    list_sources,
    delete_source,
)

router = APIRouter()


@router.post("/v1/files")
def upload_file(file: UploadFile = File(...), user: Authed = Depends(get_current_user)):
    """
    Upload a file, extract text, chunk, and upsert into the user's RAG index.
    Accepts PDF / DOCX / TXT.
    """
    try:
        with NamedTemporaryFile(delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read upload: {e!s}")

    source = file.filename or "uploaded"

    try:
        extracted = extract_text_from_any(tmp_path, source)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction failed: {e!s}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    text = normalize_text(extracted.get("text", "") or "")
    if not text:
        raise HTTPException(
            status_code=400,
            detail=f"Could not extract meaningful text from {source}. Try uploading as PDF or TXT.",
        )

    chunks = smart_chunk(text, target_tokens=300, overlap_tokens=40)
    if not chunks:
        raise HTTPException(status_code=400, detail="Text chunking produced no content")

    try:
        n = upsert_chunks_for_source(
            user_id=user.user_id,
            source=source,
            chunks=chunks,
            metadata=extracted.get("meta") or {},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Index upsert failed: {e!s}")

    return {"ok": True, "source": source, "chunks_added": n, "meta": extracted.get("meta") or {}}


@router.get("/v1/files")
def list_files(user: Authed = Depends(get_current_user)):
    """
    List distinct sources (filenames) with chunk counts and file_id.
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
            return {"ok": True, "removed": 0, "message": "No chunks matched this source."}
        return {"ok": True, "removed": removed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e!s}")
