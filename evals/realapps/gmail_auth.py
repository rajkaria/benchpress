"""OAuth bearer tokens for the Gmail scratch account.

`GMAIL_ACCESS_TOKEN` is used directly when set (handy for a one-off token from the OAuth Playground).
Otherwise the provider exchanges `GMAIL_REFRESH_TOKEN` at Google's token endpoint with
`GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`, caches the access token, and refreshes it about a minute before
it expires. The scratch mailbox address itself comes from `GMAIL_ADDRESS`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
REFRESH_SKEW_SECONDS = 60.0
DEFAULT_EXPIRES_IN = 3600.0

HeadersProvider = Callable[[], Awaitable[Mapping[str, str]]]


class GmailAuthError(RuntimeError):
    """Missing credentials or a failed token exchange."""


class GmailTokenProvider:
    """Caches one access token and refreshes it through the refresh-token grant when needed."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        token_url: str = TOKEN_URL,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        source = os.environ if env is None else env
        self._direct_token = source.get("GMAIL_ACCESS_TOKEN", "").strip() or None
        self._client_id = source.get("GMAIL_CLIENT_ID", "").strip()
        self._client_secret = source.get("GMAIL_CLIENT_SECRET", "").strip()
        self._refresh_token = source.get("GMAIL_REFRESH_TOKEN", "").strip()
        if self._direct_token is None and not (self._client_id and self._client_secret and self._refresh_token):
            raise GmailAuthError(
                "set GMAIL_ACCESS_TOKEN, or GMAIL_CLIENT_ID + GMAIL_CLIENT_SECRET + GMAIL_REFRESH_TOKEN, "
                "for the Gmail scratch account"
            )
        self._token_url = token_url
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._now = now
        self._cached: str | None = self._direct_token
        self._expires_at: float | None = None  # None: never expires (direct token)
        self.refresh_count = 0

    @property
    def uses_direct_token(self) -> bool:
        return self._direct_token is not None

    def _fresh(self) -> bool:
        if self._cached is None:
            return False
        if self._expires_at is None:
            return True
        return self._now() < self._expires_at - REFRESH_SKEW_SECONDS

    async def token(self) -> str:
        if self._fresh():
            assert self._cached is not None
            return self._cached
        return await self._refresh()

    async def _refresh(self) -> str:
        response = await self._client.post(
            self._token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
        )
        if response.status_code != 200:
            raise GmailAuthError(f"Gmail token refresh failed: HTTP {response.status_code} {response.text[:200]}")
        payload: object = response.json()
        if not isinstance(payload, Mapping):
            raise GmailAuthError("Gmail token endpoint returned a non-object payload")
        typed = cast(Mapping[str, Any], payload)
        access_token: object = typed.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GmailAuthError("Gmail token endpoint returned no access_token")
        expires_in: object = typed.get("expires_in", DEFAULT_EXPIRES_IN)
        seconds = float(expires_in) if isinstance(expires_in, int | float | str) else DEFAULT_EXPIRES_IN
        self._cached = access_token
        self._expires_at = self._now() + seconds
        self.refresh_count += 1
        return access_token

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token()}"}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def gmail_headers_provider(
    env: Mapping[str, str] | None = None, *, client: httpx.AsyncClient | None = None
) -> HeadersProvider:
    """A `GmailApp`-compatible headers provider that refreshes its token as needed."""
    return GmailTokenProvider(env, client=client).headers


def gmail_address(env: Mapping[str, str] | None = None) -> str:
    """The scratch mailbox address seeded messages are delivered to (`GMAIL_ADDRESS`)."""
    source = os.environ if env is None else env
    address = source.get("GMAIL_ADDRESS", "").strip()
    if "@" not in address:
        raise GmailAuthError("set GMAIL_ADDRESS to the scratch Gmail account's address")
    return address
