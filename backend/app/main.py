from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.routers import analysis, auth, lobby, session, spotify
from app.services import embedding_service, popularity_service

configure_logging()
log = get_logger("startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the heavy singletons once at boot (embedding vectors + popularity parquet, ~seconds
    # each) so the first Build Profile / Start Game doesn't eat the load mid-request.
    log.info("warming embedding model + popularity parquet…")
    embedding_service._load()
    popularity_service._load()
    log.info("warmup complete")
    yield


app = FastAPI(title="hitster-maker API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth/spotify", tags=["auth"])
app.include_router(spotify.router, prefix="/spotify", tags=["spotify"])
app.include_router(lobby.router, prefix="/lobby", tags=["lobby"])
app.include_router(session.router, prefix="/session", tags=["session"])
app.include_router(analysis.router, prefix="/analysis", tags=["analysis"])


@app.get("/health")
async def health():
    return {"status": "ok"}
