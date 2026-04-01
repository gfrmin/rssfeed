from urllib.parse import unquote

from fastapi import APIRouter
from fastapi.responses import Response

from app.extractor import fetch_proxied_image

router = APIRouter()


@router.get("/proxy/image")
async def proxy_image(url: str):
    """Proxy an image through the sidecar to avoid tracking and hotlink issues."""
    decoded = unquote(url)
    result = await fetch_proxied_image(decoded)
    if not result:
        return Response(status_code=404)
    data, content_type = result
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
