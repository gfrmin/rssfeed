import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.db import run_migrations
from app.routes import entries, feeds, stats, filters, share, proxy, digest
from app.worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    task = asyncio.create_task(worker_loop())
    yield
    task.cancel()


app = FastAPI(title="RSS Sidecar", lifespan=lifespan)

static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(feeds.router)
app.include_router(entries.router)
app.include_router(stats.router)
app.include_router(filters.router)
app.include_router(share.router)
app.include_router(proxy.router)
app.include_router(digest.router)
