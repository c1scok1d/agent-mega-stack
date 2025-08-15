from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.api.billing import router as billing_router
from app.api.chat import router as chat_router
from app.api.files import router as files_router
from app.api.usage import router as usage_router
from app.api.search import router as search_router
from app.api.tools import router as tools_router
from app.api.agents import router as agents_router
from app.api.agents_tools import router as agent_tools_router
from app.api.admin import router as admin_router
# ...


app = FastAPI(title="Agent Mega Stack API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(billing_router)
app.include_router(chat_router)   # /v1/chat and /v1/chat/stream
app.include_router(files_router)  # /v1/files
app.include_router(usage_router)
app.include_router(search_router) # /v1/search
app.include_router(tools_router)
app.include_router(agents_router)
app.include_router(agent_tools_router)
app.include_router(admin_router)

@app.get("/openapi.json")
def openapi_json():
    return app.openapi()
