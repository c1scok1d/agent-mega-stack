import re, json
from typing import TypedDict, List, Dict, Optional
from pydantic import BaseModel, ValidationError
from langgraph.graph import StateGraph, END
from langchain.schema import SystemMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI
from httpx import Timeout
from openai import APIConnectionError, RateLimitError
from app.agents.policy import SYSTEM
from app.core.settings import settings


class ToolCall(BaseModel):
    tool: str
    args: dict = {}


class AgentState(TypedDict, total=False):
    messages: List[Dict]
    summary: Optional[str]


def _maybe_tool_call(text: str) -> Optional[ToolCall]:
    m = re.search(r"\{.*\}\s*$", text.strip(), re.S)
    if not m:
        return None
    try:
        return ToolCall.model_validate_json(m.group(0))
    except ValidationError:
        return None


def _mk_msgs(state: AgentState, system_override: Optional[str] = None):
    sys_txt = (system_override or SYSTEM)
    msgs = [SystemMessage(content=sys_txt)]
    for m in state["messages"]:
        cls = {"user": HumanMessage, "assistant": AIMessage, "system": SystemMessage}[m["role"]]
        msgs.append(cls(content=m["content"]))
    return msgs


def make_graph(tools: dict, n_predict: int | None = None, system_override: Optional[str] = None, temperature: float = 0.2, stream_handler=None):
    """
    n_predict is kept for backward-compat, but is mapped to max_tokens (OpenAI-style).
    """
    g = StateGraph(AgentState)

    def _llm(streaming: bool = False):
        callbacks = [stream_handler] if (streaming and stream_handler) else None
        # For llama.cpp-compatible backends, pass generation params in extra_body
        extra = {"n_predict": n_predict if n_predict is not None else 256,
                 "stop": ["<|im_end|>"]}
        # llama.cpp OpenAI-compatible server supports extras via model_kwargs.
        mk = {"stop": ["<|im_end|>"]}
        if n_predict is not None:
            mk["n_predict"] = n_predict
        # IMPORTANT: use openai_api_base / openai_api_key for langchain-openai
        # and standard OpenAI params (max_tokens, temperature). Do NOT pass llama.cpp flags.
        return ChatOpenAI(
            model=settings.MODEL_NAME,
            openai_api_base=settings.OPENAI_BASE_URL,   # e.g. http://127.0.0.1:8081/v1
            openai_api_key=settings.OPENAI_API_KEY,     # any non-empty string for llama.cpp
            temperature=0.2,
            streaming=bool(callbacks),
            callbacks=callbacks,
            timeout=Timeout(15.0, read=60.0),
            extra_body = extra,
            max_tokens=(n_predict if n_predict is not None else 256),
        )

    def call_model(state: AgentState) -> AgentState:
        try:
            out = _llm(streaming=bool(stream_handler)).invoke(_mk_msgs(state)).content
        except APIConnectionError:
            state.setdefault("messages", []).append({"role": "assistant", "content": "(LLM unavailable)"})
            return state
        except RateLimitError:
            state.setdefault("messages", []).append({"role": "assistant", "content": "(LLM overloaded)"})
            return state

        state["messages"].append({"role": "assistant", "content": out})
        return state

    def maybe_route(state: AgentState) -> str:
        last = state["messages"][-1]["content"]
        return "tool" if _maybe_tool_call(last) else END

    def run_tool(state: AgentState) -> AgentState:
        last = state["messages"][-1]["content"]
        call = _maybe_tool_call(last)
        res = {"error": "unknown tool"}
        if call and (t := tools.get(call.tool)):
            res = t.run(**call.args)

        follow = [
            {"role": "system", "content": "Tool result provided as JSON below."},
            {"role": "tool", "content": json.dumps(res)},
        ]
        # Compact context before the follow-up LLM call:
        compact_state = {"messages": (state["messages"][-6:] + follow)}

        # If you need stop tokens, pass as an explicit kwarg here too
        # final_msg = _llm(streaming=False).invoke(_mk_msgs(compact_state), stop=["<|im_end|>"])
        final_msg = _llm(streaming=bool(stream_handler)).invoke(_mk_msgs(compact_state))
        final = final_msg.content

        state["messages"].append({"role": "tool", "content": json.dumps(res)})
        state["messages"].append({"role": "assistant", "content": final})
        return state

    g.add_node("model", call_model)
    g.add_node("tool", run_tool)
    g.add_edge("tool", END)
    g.add_conditional_edges("model", maybe_route, {"tool": "tool", END: END})
    g.set_entry_point("model")
    return g.compile()
