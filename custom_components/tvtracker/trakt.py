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

    # ---- writing (additions and hiding only; this client never deletes history) ----
    async def _get_list(self, path: str, token: str) -> list[dict[str, Any]]:
        status, body = await self._request("GET", path, token=token)
        if status in (401, 403):
            raise TraktAuthError("Trakt rejected the access token")
        if status != 200 or not isinstance(body, list):
            raise TraktError(f"Could not read {path} (HTTP {status})")
        return body

    async def _post(self, path: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        status, data = await self._request("POST", path, token=token, json=body)
        if status in (401, 403):
            raise TraktAuthError("Trakt rejected the access token")
        if status not in (200, 201):
            raise TraktError(f"Trakt refused {path} (HTTP {status})")
        return data if isinstance(data, dict) else {}

    async def _get_object(self, path: str, token: str, params: dict[str, Any] | None = None) -> Any:
        status, body = await self._request("GET", path, token=token, params=params)
        if status in (401, 403):
            raise TraktAuthError("Trakt rejected the access token")
        if status == 404:
            return None
        if status != 200:
            raise TraktError(f"Could not read {path} (HTTP {status})")
        return body

    async def find_show(self, token: str, tmdb_id: int) -> int | None:
        """Trakt's own id for a show, from its TMDB id (None if Trakt doesn't know it)."""
        found = await self._get_object(f"/search/tmdb/{int(tmdb_id)}", token, {"type": "show"})
        for hit in found or []:
            trakt_id = ((hit.get("show") or {}).get("ids") or {}).get("trakt")
            if trakt_id:
                return int(trakt_id)
        return None

    async def show_progress(self, token: str, trakt_id: int) -> dict[str, Any]:
        """Trakt's own record of which episodes of a show you've completed."""
        data = await self._get_object(
            f"/shows/{int(trakt_id)}/progress/watched",
            token,
            {"hidden": "false", "specials": "false"},
        )
        if not isinstance(data, dict):
            raise TraktError(f"Trakt has no progress for show {trakt_id}")
        return data

    async def watched_movies(self, token: str) -> list[dict[str, Any]]:
        return await self._get_list("/sync/watched/movies", token)

    async def watched_shows(self, token: str) -> list[dict[str, Any]]:
        """Every show you've watched any of, with the seasons and episodes watched."""
        return await self._get_list("/sync/watched/shows", token)

    async def add_history(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Add watches: {"shows": [...], "movies": [...]}. Returns Trakt's added/not_found."""
        return await self._post("/sync/history", token, payload)

    async def hide(self, token: str, section: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Hide shows from a Trakt section (progress_watched, calendar, ...)."""
        return await self._post(f"/users/hidden/{section}", token, payload)

    async def unhide(self, token: str, section: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Un-hide (removes the *hide*, never any history)."""
        return await self._post(f"/users/hidden/{section}/remove", token, payload)

    async def search_episodes(self, query: str, token: str | None = None) -> list[dict[str, Any]]:
        """Trakt's search across every show's episode titles (TMDB can't do this)."""
        status, body = await self._request(
            "GET", "/search/episode", token=token, params={"query": query, "limit": 10}
        )
        if status in (401, 403) and token:
            raise TraktAuthError("Trakt rejected the access token")
        if status != 200 or not isinstance(body, list):
            raise TraktError(f"Trakt episode search failed (HTTP {status})")
        return body
