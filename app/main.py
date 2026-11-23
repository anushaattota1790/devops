import os

from fastapi import FastAPI

app = FastAPI(title="healthz-service", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    # APP_VERSION is baked into the image at build time (git SHA).
    return {"status": "ok", "version": os.getenv("APP_VERSION", "dev")}
