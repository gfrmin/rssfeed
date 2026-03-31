import os

DATABASE_URL = os.environ["DATABASE_URL"]
MINIFLUX_URL = os.environ.get("MINIFLUX_URL", "http://miniflux:8080")
MINIFLUX_API_KEY = os.environ.get("MINIFLUX_API_KEY", "")
BRIGHTDATA_PROXY = os.environ.get("BRIGHTDATA_PROXY", "")
WORKER_POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", "60"))
