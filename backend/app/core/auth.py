"""Shared request-auth helper for routers that take a raw Spotify Bearer token."""

from __future__ import annotations

from fastapi import HTTPException


def extract_bearer_token(authorization: str | None) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header, or 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or invalid Authorization header"
        )
    return authorization.removeprefix("Bearer ")
