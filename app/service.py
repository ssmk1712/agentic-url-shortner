from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import HTTPException

from .config import settings
from .repository import UrlRepository, repository

ALPHABET = string.ascii_letters + string.digits


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_expiry(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, "expires_at must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    normalized = parsed.astimezone(timezone.utc).isoformat()
    if normalized <= now_iso():
        raise HTTPException(422, "expires_at must be in the future")
    return normalized


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(settings.short_code_length))


def is_reserved(code: str) -> bool:
    return code.lower() in settings.reserved_aliases


class UrlService:
    def __init__(self, repo: UrlRepository = repository):
        self.repo = repo

    def shorten(self, target_url: str, custom_alias: str | None, expires_at: str | None) -> dict:
        if not valid_url(target_url):
            raise HTTPException(422, "Only valid http/https URLs are accepted")

        expiry = normalize_expiry(expires_at)
        created_at = now_iso()

        if custom_alias:
            if is_reserved(custom_alias):
                raise HTTPException(422, "Alias is reserved by the service")
            if not self.repo.create_url(custom_alias, target_url, created_at, expiry):
                raise HTTPException(409, "Alias already exists")
            code = custom_alias
        else:
            code = ""
            for _ in range(settings.short_code_attempts):
                candidate = new_code()
                if is_reserved(candidate):
                    continue
                if self.repo.create_url(candidate, target_url, created_at, expiry):
                    code = candidate
                    break
            if not code:
                raise HTTPException(503, "Could not allocate a unique short code")

        return {
            "code": code,
            "short_url": f"{settings.base_url.rstrip('/')}/{code}",
            "target_url": target_url,
            "expires_at": expiry,
        }

    def resolve(self, code: str) -> dict:
        row = self.repo.get_url(code)
        if not row or not row["active"]:
            raise HTTPException(404, "Short URL not found")
        if row["expires_at"] and row["expires_at"] <= now_iso():
            raise HTTPException(410, "Short URL expired")
        return row


service = UrlService()
