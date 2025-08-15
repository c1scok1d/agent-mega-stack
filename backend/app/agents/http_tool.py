# app/agents/http_tool.py
from __future__ import annotations
import json, re
from typing import Any, Dict, Optional
import httpx

# very light templating using Python .format(**kwargs)
# blocks {..} that aren't provided so we fail fast instead of sending "{missing}"
_MISS_RE = re.compile(r"{([^}]+)}")

def _render_tmpl(tmpl: str | None, args: Dict[str, Any]) -> str | None:
    if tmpl is None:
        return None
    needed = {m.group(1) for m in _MISS_RE.finditer(tmpl)}
    missing = [k for k in needed if k not in args]
    if missing:
        raise ValueError(f"Missing template params: {missing}")
    return tmpl.format(**args)

def _render_obj(obj: Any, args: Dict[str, Any]) -> Any:
    if obj is None:
        return None
    if isinstance(obj, str):
        return _render_tmpl(obj, args)
    if isinstance(obj, dict):
        return {k: _render_obj(v, args) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_obj(v, args) for v in obj]
    return obj

# very small allowlist control (optional)
def _is_host_allowed(url: str, allowed_hosts: list[str] | None) -> bool:
    if not allowed_hosts:
        return True
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    return any(host == h.lower() or host.endswith("." + h.lower()) for h in allowed_hosts)

class HttpTool:
    """
    Config example:
    {
      "method": "GET",                         // GET/POST/PUT/PATCH/DELETE
      "url": "https://api.example.com/users/{id}",
      "headers": {"Authorization": "Bearer {token}"},
      "query": {"q": "{query}", "limit": "5"},
      "json": {"note": "{text}"},              // or "data": "raw body string"
      "timeout_s": 15,
      "allowed_hosts": ["api.example.com"]     // optional allowlist
    }
    """
    def __init__(self, config: Dict[str, Any]):
        self.cfg = {
            "method": str(config.get("method", "GET")).upper(),
            "url": config.get("url"),
            "headers": config.get("headers"),
            "query": config.get("query"),
            "json": config.get("json"),
            "data": config.get("data"),
            "timeout_s": int(config.get("timeout_s", 15)),
            "allowed_hosts": list(config.get("allowed_hosts", [])),
        }
        if not self.cfg["url"]:
            raise ValueError("HTTP tool requires 'url'")

    def run(self, **kwargs) -> Dict[str, Any]:
        method = self.cfg["method"]
        url    = _render_tmpl(self.cfg["url"], kwargs)
        if not _is_host_allowed(url, self.cfg["allowed_hosts"]):
            return {"error": "host_not_allowed", "url": url}

        headers = _render_obj(self.cfg["headers"], kwargs)
        params  = _render_obj(self.cfg["query"], kwargs)
        json_b  = _render_obj(self.cfg["json"], kwargs)
        data_b  = _render_obj(self.cfg["data"], kwargs)

        try:
            with httpx.Client(timeout=self.cfg["timeout_s"], follow_redirects=True) as cli:
                resp = cli.request(method, url, headers=headers, params=params, json=json_b, data=data_b)
            ct = resp.headers.get("content-type", "")
            body: Any
            if "application/json" in ct:
                try:
                    body = resp.json()
                except json.JSONDecodeError:
                    body = resp.text
            else:
                # return short text, but include length
                txt = resp.text
                body = txt if len(txt) <= 4000 else (txt[:4000] + "…")

            return {
                "ok": resp.is_success,
                "status": resp.status_code,
                "headers": dict(resp.headers),
                "body": body,
                "url": str(resp.request.url),
                "method": method,
            }
        except Exception as e:
            return {"error": "request_failed", "message": str(e)}
