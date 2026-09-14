from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .db import init_db
from .repository import repository
from .service import now_iso, service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Agentic URL Shortener",
    version="1.1.0",
    description="Runnable URL shortener used as the product plane for a governed agentic SDLC prototype.",
    lifespan=lifespan,
)


class ShortenRequest(BaseModel):
    url: str
    custom_alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{3,32}$")
    expires_at: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/urls", status_code=201)
def shorten(req: ShortenRequest) -> dict:
    return service.shorten(req.url, req.custom_alias, req.expires_at)


@app.get("/{code}")
def redirect(code: str, request: Request):
    row = service.resolve(code)
    repository.record_click(code, now_iso(), request.headers.get("user-agent"), request.headers.get("referer"))
    return RedirectResponse(row["target_url"], status_code=307)


@app.get("/api/v1/urls/{code}/analytics")
def analytics(code: str) -> dict:
    result = repository.analytics(code)
    if not result:
        raise HTTPException(404, "Short URL not found")
    return result


@app.delete("/api/v1/urls/{code}", status_code=204)
def deactivate(code: str) -> None:
    if not repository.deactivate_url(code):
        raise HTTPException(404, "Short URL not found")
