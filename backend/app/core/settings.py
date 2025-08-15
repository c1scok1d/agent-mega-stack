
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    OPENAI_BASE_URL: str = "http://127.0.0.1:8081/v1"
    OPENAI_API_KEY: str = "x"
    MODEL_NAME: str = "llama-2-7b-chat"
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/agentstack"
    JWT_SECRET: str = "dev-secret"
    JWT_ALG: str = "HS256"
    JWT_TTL_MIN: int = 60
    REFRESH_BYTES: int = 32
    CORS_ORIGINS: str = "*"
    # choose HF embeddings
    EMBEDDINGS_BACKEND: str = "hf"
    EMBEDDINGS_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    RAG_STORE_DIR: str = ".rag_store"

    #EMBEDDINGS_BACKEND=llama
    #EMBEDDINGS_MODEL=text-embedding-3-small     # name doesn’t matter to llama.cpp, but LangChain requires a string
settings = Settings()
