"""Minimal async Trakt client (device-code login + watch history)."""

from __future__ import annotations

from typing import Any

import aiohttp

BASE = "https://api.trakt.tv"
REDIRECT_OOB = "urn:ietf:wg:oauth:2.0:oob"
PAGE_SIZE = 100
MAX_PAGES = 20


class TraktError(Exception):
    """Trakt could not be reached or rejected a request."""


class TraktAuthError(TraktError):
    """The token or client credentials are no longer accepted."""


class TraktClient:
    def __init__(
        self, session: aiohttp.ClientSession, client_id: str, client_secret: str
    ) -> None:
        self._session = session
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        try:
            async with self._session.request(
                method,
                f"{BASE}{path}",
                headers=self._headers(token),
                json=json,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                return resp.status, body
        except aiohttp.ClientError as err:
            raise TraktError(f"Could not reach Trakt: {err}") from err

    # ---- device-code login ------------------------------------------------
    async def device_code(self) -> dict[str, Any]:
        """Start login. Returns user_code, verification_url, device_code, ..."""
        status, body = await self._request(
            "POST", "/oauth/device/code", json={"client_id": self.client_id}
        )
        if status != 200 or not body:
            raise TraktError(f"Trakt refused to start login (HTTP {status})")
        return body

    async def poll_token(self, device_code: str) -> tuple[str, dict[str, Any] | None]:
        """One poll of the login. Returns (state, tokens).

        state: 'ok' | 'pending' | 'slow_down' | 'expired' | 'denied' | 'invalid'.
        """
        status, body = await self._request(
            "POST",
            "/oauth/device/token",
            json={
                "code": device_code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if status == 200 and body:
            return "ok", body
        states = {
            400: "pending",
            429: "slow_down",
            410: "expired",
            418: "denied",
            404: "invalid",
            409: "invalid",
        }
        if status in states:
            return states[status], None
        raise TraktError(f"Unexpected reply from Trakt while logging in (HTTP {status})")

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        status, body = await self._request(
            "POST",
            "/oauth/token",
            json={
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": REDIRECT_OOB,
                "grant_type": "refresh_token",
            },
        )
        if status in (400, 401, 403):
            raise TraktAuthError("Trakt no longer accepts the saved login; reconnect it")
        if status != 200 or not body:
            raise TraktError(f"Could not refresh the Trakt login (HTTP {status})")
        return body

    # ---- data ---------------------------------------------------------------
    async def history(self, token: str, start_at: str | None) -> list[dict[str, Any]]:
        """Everything watched since `start_at` (ISO 8601), all pages, newest first."""
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            params: dict[str, Any] = {"page": page, "limit": PAGE_SIZE}
            if start_at:
                params["start_at"] = start_at
            status, body = await self._request(
                "GET", "/sync/history", token=token, params=params
            )
            if status in (401, 403):
                raise TraktAuthError("Trakt rejected the access token")
            if status != 200 or not isinstance(body, list):
                raise TraktError(f"Could not read Trakt history (HTTP {status})")
            out.extend(body)
            if len(body) < PAGE_SIZE:
                break
        return out
