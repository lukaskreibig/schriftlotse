from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(slots=True)
class GNDClient:
    timeout: float = 8.0
    _cache: dict[str, tuple[float, list[dict[str, Any]]]] = field(default_factory=dict)

    def search_person(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        key = name.strip().casefold()
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < 86400:
            return cached[1]
        response = httpx.get(
            "https://lobid.org/gnd/search",
            params={
                "q": f'preferredName:"{name}" OR variantName:"{name}"',
                "filter": "type:Person",
                "format": "json",
                "size": min(limit, 20),
            },
            headers={"Accept": "application/json", "User-Agent": "SchriftLotse/0.1"},
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        members = response.json().get("member", [])
        results = []
        for item in members:
            results.append(
                {
                    "gnd_id": str(item.get("id", "")).rsplit("/", 1)[-1],
                    "name": item.get("preferredName", ""),
                    "varianten": item.get("variantName", []),
                    "geburt": item.get("dateOfBirth", []),
                    "tod": item.get("dateOfDeath", []),
                    "beruf": [
                        entry.get("label", "") for entry in item.get("professionOrOccupation", [])
                    ],
                    "url": item.get("id", ""),
                    "same_as": [entry.get("id", "") for entry in item.get("sameAs", [])],
                }
            )
        self._cache[key] = (time.time(), results)
        return results
