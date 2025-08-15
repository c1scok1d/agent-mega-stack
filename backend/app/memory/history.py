
from typing import List, Dict, Tuple
from langchain_openai import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage
MAX_TURNS = 8
def _llm():
    return ChatOpenAI(model="llama-2-7b-chat", base_url="http://127.0.0.1:8081/v1", api_key="x",
                      temperature=0.2, model_kwargs={"n_predict": 160, "stop": ["<|im_end|>"]})
def trim_and_summarize(messages: List[Dict], running_summary: str | None) -> Tuple[List[Dict], str | None]:
    user_assistant = [m for m in messages if m["role"] in ("user","assistant")]
    if len(user_assistant) <= MAX_TURNS: return messages, running_summary
    keep = [m for m in messages if m["role"] == "system"]
    convo = [m for m in messages if m["role"] != "system"]
    head = convo[:-MAX_TURNS]; tail = convo[-MAX_TURNS:]
    text = "\n".join(f'{m["role"]}: {m["content"]}' for m in head)
    prompt = [SystemMessage(content="Summarize <= 150 words."), HumanMessage(content=text[:8000])]
    summary = _llm().invoke(prompt).content.strip()
    if running_summary: summary = f"{running_summary}\n\nUpdate:\n{summary}"
    trimmed = keep + [{"role":"system","content": f"[Summary]\n{summary}"}] + tail
    return trimmed, summary
