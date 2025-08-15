# app/api/search.py
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from app.core.security import Authed, get_current_user
from app.rag.index import rag_search

router = APIRouter()

class SearchIn(BaseModel):
    query: str
    k: int = 5

@router.post("/v1/search")
def search_docs(body: SearchIn, user: Authed = Depends(get_current_user)):
    hits = rag_search(user.user_id, body.query, k=body.k)
    return [
        {"content": d.page_content, "source": d.metadata.get("source")}
        for d in hits
    ]
