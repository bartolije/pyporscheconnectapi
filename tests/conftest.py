"""Shared pytest fixtures."""
from __future__ import annotations

import time

import httpx
import pytest

from pyporscheconnectapi.connection import Connection


@pytest.fixture
def email() -> str:
    return "user@example.com"


@pytest.fixture
def password() -> str:
    return "s3cret"


@pytest.fixture
async def connection(email: str, password: str):
    """An unauthenticated Connection sharing a single httpx.AsyncClient.

    The shared client lets respx intercept every HTTP call made by the lib
    in the test, and lets the OAuth flow keep its cookies across requests.
    """
    async with httpx.AsyncClient() as client:
        conn = Connection(email=email, password=password, async_client=client)
        try:
            yield conn
        finally:
            await conn.close()


@pytest.fixture
async def authed_connection(email: str, password: str):
    """A Connection pre-seeded with a valid (non-expired) OAuth2 token.

    Avoids respx-mocking the full OAuth dance in tests that only care about
    the application-level API calls (vehicles, commands, etc.).
    """
    token = {
        "access_token": "test.access.token",
        "refresh_token": "test.refresh.token",
        "expires_at": int(time.time()) + 3600,
        "token_type": "Bearer",
    }
    async with httpx.AsyncClient() as client:
        conn = Connection(
            email=email, password=password, async_client=client, token=token,
        )
        try:
            yield conn
        finally:
            await conn.close()
