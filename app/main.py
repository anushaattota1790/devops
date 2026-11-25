import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.ready = True
    try:
        yield
    finally:
        app.state.ready = False


app = FastAPI(title="healthz-service", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.state.ready = False


@app.get("/healthz")
def healthz() -> dict[str, str | None]:
    # APP_VERSION is baked into the image at build time.
    return {
        "status": "ok",
        "version": os.getenv("APP_VERSION", "dev"),
        "previous_version": os.getenv("PREVIOUS_APP_VERSION") or None,
    }


@app.get("/readyz")
def readyz(response: Response) -> dict[str, str]:
    if not app.state.ready:
        response.status_code = 503
        return {"status": "not ready"}
    return {"status": "ready"}
