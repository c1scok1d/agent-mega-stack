
# Agent Mega Stack (Apply-and-Run)

## Requirements
- Docker + Docker Compose
- `uv` and `llama.cpp` CLI (`brew install uv llama.cpp` on macOS)
- A GGUF model (e.g., llama-2-7b-chat.Q4_K_M.gguf)

## Run
```bash
cp .env.example .env
MODEL_PATH=/path/to/llama-2-7b-chat.Q4_K_M.gguf ./dev up
```

## Endpoints
- `POST /v1/auth/signup` → `{jwt, refresh_token}`
- `POST /v1/auth/login`
- `POST /v1/auth/refresh`
- `POST /v1/chat` `{session_id,message}` (JWT required)
- `GET  /v1/usage` (JWT required)
- `POST /v1/files` (multipart)
- `POST /v1/billing/checkout` (Stripe optional)
- `POST /v1/billing/webhook`

## Stop
```bash
./dev down
```
