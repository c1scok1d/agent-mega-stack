# backend/app/api/chat.py
from __future__ import annotations

import asyncio
import json
from typing import Dict, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain.callbacks.streaming_aiter import AsyncIteratorCallbackHandler

from app.agents.graph import make_graph
from app.agents.tool_runtime import build_tools_for_user
from app.core.security import Authed
from app.billing.deps import require_active_subscription, record_usage
from app.billing.limits import enforce_rate_limit
from app.memory.history import trim_and_summarize

router = APIRouter()

# In-memory per-user, per-session state
# key = (user_id, session_id) -> {"messages": [...], "summary": str|None}
SESS: Dict[Tuple[str, str], Dict] = {}


class ChatIn(BaseModel):
    session_id: str
    message: str


@router.post("/v1/chat")
def chat(
    body: ChatIn,
    user: Authed = Depends(require_active_subscription),
    limits: dict = Depends(enforce_rate_limit),
):
    """
    Non-streaming chat endpoint. Runs the graph and returns the assistant reply.
    """
    key = (user.user_id, body.session_id)
    st = SESS.get(key, {"messages": [], "summary": None})

    # Add user message + compact long histories
    st["messages"].append({"role": "user", "content": body.message})
    st["messages"], st["summary"] = trim_and_summarize(st["messages"], st.get("summary"))

    # Tools and graph
    tools = build_tools_for_user(user.user_id, include_defaults=True)
    graph = make_graph(tools, n_predict=limits.get("n_predict"))

    # Run graph synchronously
    out = graph.invoke(st)
    SESS[key] = out

    # Extract last assistant message
    reply = next(m for m in reversed(out["messages"]) if m["role"] == "assistant")["content"]

    # Naive token estimate for usage metering
    tokens = max(1, len(body.message) // 4 + len(reply) // 4)
    record_usage(user.user_id, tokens=tokens, requests=1)

    return {"reply": reply}


@router.post("/v1/chat/stream")
async def chat_stream(
    body: ChatIn,
    user: Authed = Depends(require_active_subscription),
    limits: dict = Depends(enforce_rate_limit),
):
    """
    Streaming chat endpoint (SSE). Tokens are streamed as they are produced.
    """
    key = (user.user_id, body.session_id)
    st = SESS.get(key, {"messages": [], "summary": None})

    # Add user message + compact long histories
    st["messages"].append({"role": "user", "content": body.message})
    st["messages"], st["summary"] = trim_and_summarize(st["messages"], st.get("summary"))

    # Hook a streaming handler into the graph's LLM calls
    handler = AsyncIteratorCallbackHandler()
    tools = build_tools_for_user(user.user_id, include_defaults=True)
    graph = make_graph(tools, n_predict=limits.get("n_predict"), stream_handler=handler)

    async def run_graph():
        """
        Run the (sync) graph on a worker thread; close the async iterator on completion.
        """
        try:
            out = await asyncio.to_thread(graph.invoke, st)
            SESS[key] = out
        finally:
            await handler.aiterator.aclose()

    async def sse():
        """
        Forward each token from the LangChain async iterator as an SSE frame.
        """
        task = asyncio.create_task(run_graph())
        # buffer the full streamed text as fallback in case we can't read it from session
        assembled = ""
        async for token in handler.aiter():
            assembled += token
            yield f"data: {json.dumps({'delta': token})}\n\n"
        await task

        # finalize usage metering once we have the full assistant output
        try:
            out = SESS.get(key, st)
            reply = next(
                (m["content"] for m in reversed(out["messages"]) if m["role"] == "assistant"),
                assembled,
            )
            tokens = max(1, len(body.message) // 4 + len(reply) // 4)
            record_usage(user.user_id, tokens=tokens, requests=1)
        except Exception:
            # don't fail the stream on accounting issues
            pass

        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
