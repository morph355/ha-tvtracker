"""Minimal async TMDB client."""

from __future__ import annotations

from typing import Any

import aiohttp

from .logic import parse_details, parse_providers

BASE = "https://api.themoviedb.org/3"


class TMDBError(Exception):
    """Raised when TMDB can't be reached or rejects a request."""


class TMDBAuthError(TMDBError):
    """Raised for a bad API key."""


class TMDB:
    def __init__(self, session: aiohttp.ClientSession, api_key: str, region: str) -> None:
        self._session = session
        self._key = api_key.strip()
        self.region = region

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        # v4 read tokens are JWTs; v3 keys are 32 hex characters.
        if self._key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self._key}"
        else:
            params["api_key"] = self._key
        try:
            async with self._session.get(
                f"{BASE}{path}",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    raise TMDBAuthError("TMDB rejected the API key")
                if resp.status != 200:
                    raise TMDBError(f"TMDB returned HTTP {resp.status} for {path}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise TMDBError(f"Could not reach TMDB: {err}") from err

    async def validate(self) -> None:
        await self._get("/configuration")

    async def search(self, query: str, media_type: str | None = None) -> list[dict[str, Any]]:
        """Search TMDB. media_type: 'tv', 'movie' or None for both."""
        kinds = [media_type] if media_type else ["tv", "movie"]
        results: list[dict[str, Any]] = []
        for kind in kinds:
            data = await self._get(f"/search/{kind}", query=query, language="en-GB")
            for r in (data.get("results") or [])[:6]:
                date = r.get("first_air_date") or r.get("release_date") or ""
                results.append(
                    {
                        "tmdb_id": r["id"],
                        "media_type": kind,
                        "title": r.get("name") or r.get("title"),
                        "year": date[:4] or None,
                        "overview": (r.get("overview") or "")[:160],
                        "popularity": r.get("popularity", 0),
                    }
                )
        results.sort(key=lambda r: r["popularity"], reverse=True)
        return results

    async def details(
        self, media_type: str, tmdb_id: int
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """Return (parsed details, this region's providers)."""
        raw = await self._get(
            f"/{media_type}/{int(tmdb_id)}",
            language="en-GB",
            append_to_response="watch/providers",
        )
        return parse_details(media_type, raw), parse_providers(raw, self.region)

    async def season_episodes(self, tmdb_id: int, season: int) -> list[dict[str, Any]]:
        """The episodes of one season: number, name, air date."""
        data = await self._get(f"/tv/{int(tmdb_id)}/season/{int(season)}", language="en-GB")
        return [
            {
                "episode": int(e["episode_number"]),
                "name": e.get("name") or "",
                "air_date": e.get("air_date") or None,
            }
            for e in data.get("episodes") or []
            if e.get("episode_number")
        ]
