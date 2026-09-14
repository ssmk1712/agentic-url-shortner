from __future__ import annotations

import sqlite3
from typing import Any

from .db import connection


class UrlRepository:
    """Persistence boundary for URL and click data."""

    def get_url(self, code: str) -> dict[str, Any] | None:
        with connection() as conn:
            row = conn.execute("SELECT * FROM urls WHERE code=?", (code,)).fetchone()
            return dict(row) if row else None

    def create_url(self, code: str, target_url: str, created_at: str, expires_at: str | None) -> bool:
        """Atomically attempt to create a URL mapping.

        Returns False on a primary-key collision. This avoids a check-then-insert race for
        both custom aliases and generated short codes.
        """
        try:
            with connection() as conn:
                conn.execute(
                    "INSERT INTO urls(code,target_url,created_at,expires_at) VALUES(?,?,?,?)",
                    (code, target_url, created_at, expires_at),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def deactivate_url(self, code: str) -> bool:
        with connection() as conn:
            cur = conn.execute("UPDATE urls SET active=0 WHERE code=?", (code,))
            return bool(cur.rowcount)

    def record_click(self, code: str, clicked_at: str, user_agent: str | None, referrer: str | None) -> None:
        with connection() as conn:
            conn.execute(
                "INSERT INTO clicks(code,clicked_at,user_agent,referrer) VALUES(?,?,?,?)",
                (code, clicked_at, user_agent, referrer),
            )

    def analytics(self, code: str) -> dict[str, Any] | None:
        with connection() as conn:
            url = conn.execute("SELECT * FROM urls WHERE code=?", (code,)).fetchone()
            if not url:
                return None
            count = conn.execute("SELECT COUNT(*) n FROM clicks WHERE code=?", (code,)).fetchone()["n"]
            recent = [
                dict(row)
                for row in conn.execute(
                    "SELECT clicked_at,user_agent,referrer FROM clicks WHERE code=? ORDER BY id DESC LIMIT 20",
                    (code,),
                )
            ]
            return {
                "code": code,
                "target_url": url["target_url"],
                "clicks": count,
                "recent_clicks": recent,
            }


repository = UrlRepository()
