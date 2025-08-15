
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.settings import settings
def add_cors(app: FastAPI):
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)
