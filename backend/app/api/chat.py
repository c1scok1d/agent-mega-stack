# backend/app/api/chat.py
from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Literal, Optional, Tuple

import requests
from backend.app.api.agents import SESS
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain.callbacks.streaming_aiter import AsyncIteratorCallbackHandler

from app.agents.graph import make_graph
from app.agents.tool_runtime import build_tools_for_user
from app.core.security import Authed, get_current_user
from app.billing.deps import require_active_subscription, record_usage
from app.billing.limits import enforce_rate_limit
from app.memory.history import trim_and_summarize
from app.rag.index import rag_search

router = APIRouter()

# ----------------------------
# RAG-enabled agent chat
# ----------------------------

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatIn(BaseModel):
    messages: List[ChatMessage]
    use_rag: bool = True
    k: int = 6
    max_context_chars: int = 5000

LLAMA_BASE = "http://127.0.0.1:8081/v1"

def call_llama_chat(
    messages: list[dict],
    model: str = "local-chat",
    max_tokens: int = 512,          # lower default
    temperature: float = 0.2,
    timeout_sec: int = 300          # bump timeout
) -> str:
    import requests
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    try:
        r = requests.post(f"{LLAMA_BASE}/chat/completions", json=payload, timeout=timeout_sec)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except requests.Timeout as e:
        # allow upstream handler to catch with a helpful message
        raise HTTPException(504, f"LLM call timed out after {timeout_sec}s")
    except Exception as e:
        raise HTTPException(500, f"LLM call failed: {e}")

@router.post("/v1/agents/{agent_id}/chat")
def agent_chat(agent_id: str, body: ChatIn, user: Authed = Depends(get_current_user)):
    # 1) last user message
    try:
        last_user_msg = next(m.content for m in reversed(body.messages) if m.role == "user")
    except StopIteration:
        raise HTTPException(400, "No user message found")

    used_sources = []
    # 2) build RAG context (lighter)
    def build_context(k: int, max_chars: int) -> str:
        if body.use_rag:
            try:
                hits = rag_search(user.user_id, last_user_msg, k=body.k)
            except Exception as e:
                raise HTTPException(500, f"RAG search failed: {e}")

            parts = []
            total = 0
            for i, h in enumerate(hits, 1):
                meta = (h.get("metadata") or {})
                src = meta.get("source") or "uploaded"
                snippet = (h.get("text") or "").strip()
                if not snippet:
                    continue
                if total + len(snippet) > body.max_context_chars:
                    snippet = snippet[: max(body.max_context_chars - total, 0)]
                parts.append(f"[{i}] Source: {src}\n{snippet}")
                total += len(snippet)
                used_sources.append(src)
                if total >= body.max_context_chars:
                    break

            if parts:
                context_block = (
                    "You must answer **using only** the following uploaded context. "
                    "Cite snippets by their bracketed number like [1], [2].\n\n"
                    + "\n\n---\n\n".join(parts)
                    + "\n\n---\n\nIf an answer isn’t directly supported by the snippets, say you can’t find it."
                )
        return context_block

    # First pass: smaller k and context
    k_primary = min(body.k, 4)
    max_ctx_primary = min(body.max_context_chars, 1500)
    context_block = build_context(k_primary, max_ctx_primary)

    system_prompt = (
        "You are a resume analysis assistant. Answer concisely and be specific. "
        "When context is provided, only use that context."
    )
    base_msgs: list[dict] = [{"role": "system", "content": system_prompt}]
    if context_block:
        base_msgs.append({"role": "system", "content": f"CONTEXT FOR THIS QUESTION:\n{context_block}"})
    for m in body.messages:
        if m.role in ("user", "assistant"):
            base_msgs.append({"role": m.role, "content": m.content})

    # 3) Try the call; if it times out, retry with even smaller context
    try:
        reply = call_llama_chat(base_msgs, max_tokens=256, temperature=0.2, timeout_sec=300)
        return {"reply": reply, "agent_id": agent_id, "used_rag": bool(context_block)}
    except HTTPException as e:
        if e.status_code != 504:  # not a timeout, rethrow
            raise

    # Fallback: very tiny context
    context_block = build_context(k=2, max_chars=900)
    msgs: list[dict] = [{"role": "system", "content": system_prompt}]
    if context_block:
        msgs.append({"role": "system", "content": f"CONTEXT FOR THIS QUESTION:\n{context_block}"})
    for m in body.messages:
        if m.role in ("user", "assistant"):
            msgs.append({"role": m.role, "content": m.content})

    reply = call_llama_chat(msgs, max_tokens=192, temperature=0.2, timeout_sec=300)
    return {"reply": reply, "agent_id": agent_id, "used_rag": bool(context_block),"used_sources": sorted(set(used_sources))}


@router.post("/v1/chat")
def chat_legacy(
    body: ChatIn,
    user: Authed = Depends(require_active_subscription),
    limits: dict = Depends(enforce_rate_limit),
):
    key = (user.user_id, body.session_id)
    st = SESS.get(key, {"messages": [], "summary": None})

    st["messages"].append({"role": "user", "content": body.message})
    st["messages"], st["summary"] = trim_and_summarize(st["messages"], st.get("summary"))

    tools = build_tools_for_user(user.user_id, include_defaults=True)
    graph = make_graph(tools, n_predict=limits.get("n_predict"))

    out = graph.invoke(st)
    SESS[key] = out

    reply = next(m for m in reversed(out["messages"]) if m["role"] == "assistant")["content"]
    tokens = max(1, len(body.message) // 4 + len(reply) // 4)
    record_usage(user.user_id, tokens=tokens, requests=1)
    return {"reply": reply}

@router.post("/v1/chat/stream")
async def chat_stream_legacy(
    body: ChatIn,
    user: Authed = Depends(require_active_subscription),
    limits: dict = Depends(enforce_rate_limit),
):
    key = (user.user_id, body.session_id)
    st = SESS.get(key, {"messages": [], "summary": None})

    st["messages"].append({"role": "user", "content": body.message})
    st["messages"], st["summary"] = trim_and_summarize(st["messages"], st.get("summary"))

    handler = AsyncIteratorCallbackHandler()
    tools = build_tools_for_user(user.user_id, include_defaults=True)
    graph = make_graph(tools, n_predict=limits.get("n_predict"), stream_handler=handler)

    async def run_graph():
        try:
            out = await asyncio.to_thread(graph.invoke, st)
            SESS[key] = out
        finally:
            await handler.aiterator.aclose()

    async def sse():
        task = asyncio.create_task(run_graph())
        assembled = ""
        async for token in handler.aiter():
            assembled += token
            yield f"data: {json.dumps({'delta': token})}\n\n"
        await task
        try:
            out = SESS.get(key, st)
            reply = next((m["content"] for m in reversed(out["messages"]) if m["role"] == "assistant"), assembled)
            tokens = max(1, len(body.message) // 4 + len(reply) // 4)
            record_usage(user.user_id, tokens=tokens, requests=1)
        except Exception:
            pass
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
