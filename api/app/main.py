from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .config import settings
from .routers import auth, stats, submissions, tiles, webcams


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(title="World Webcams API", version="1.0", lifespan=lifespan, root_path=settings.root_path)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webcams.router)
app.include_router(submissions.router)
app.include_router(tiles.router)
app.include_router(auth.router)
app.include_router(stats.router)


@app.get("/health", tags=["stats"])
async def health():
    await db.get_pool().fetchval("SELECT 1")
    return {"status": "ok"}
