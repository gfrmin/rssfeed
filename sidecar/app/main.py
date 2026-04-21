import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from app import miniflux_client
from app.db import run_migrations
from app.routes import cookies, entries, feeds, stats, filters, share, proxy, digest
from app.worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    await miniflux_client.startup()
    task = asyncio.create_task(worker_loop())
    try:
        yield
    finally:
        task.cancel()
        await miniflux_client.shutdown()


app = FastAPI(title="RSS Sidecar", lifespan=lifespan)


@app.middleware("http")
async def log_request_timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s %.0fms", request.method, request.url.path, dur_ms)
    return response


static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(feeds.router)
app.include_router(entries.router)
app.include_router(stats.router)
app.include_router(filters.router)
app.include_router(share.router)
app.include_router(proxy.router)
app.include_router(digest.router)
app.include_router(cookies.router)
