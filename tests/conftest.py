"""Shared pytest fixtures."""
from __future__ import annotations

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
