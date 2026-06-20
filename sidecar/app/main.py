import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from app import miniflux_client
from app.db import run_migrations
from app.routes import cookies, entries, feeds, proxy
from app.templating import templates
from app.worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    await miniflux_client.startup()
    # Warm the Credence skin (loads the model program) so the first cross-feed
    # request isn't blocked on Julia cold-start. Best-effort: the reader works
    # fine — falling back to priority+recency — if the engine isn't up.
    try:
        from app import ranker_client
        await ranker_client.load_state()
    except Exception:
        logger.exception("ranker load_state failed (continuing without ranking)")
    task = asyncio.create_task(worker_loop())
    try:
        yield
    finally:
        task.cancel()
        await miniflux_client.shutdown()
        try:
            from app import ranker_client
            await ranker_client.shutdown()
        except Exception:
            logger.exception("ranker shutdown failed")


app = FastAPI(title="RSS Sidecar", lifespan=lifespan)


@app.middleware("http")
async def log_request_timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s %.0fms", request.method, request.url.path, dur_ms)
    # Let the service worker (served from /static/) claim root scope so it controls
    # the whole app, not just /static/. Set here so it applies no matter which
    # handler (StaticFiles mount) serves the file.
    if request.url.path == "/static/sw.js":
        response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Styled 404 for browser navigation; JSON for API / non-HTML clients."""
    if exc.status_code == 404:
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html and not request.url.path.startswith("/api"):
            return templates.TemplateResponse(request, "404.html", {}, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(feeds.router)
app.include_router(entries.router)
app.include_router(proxy.router)
app.include_router(cookies.router)
