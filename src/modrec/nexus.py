"""Thin Nexus Mods API clients: v2 GraphQL (primary) and v1 REST (fallback).

See docs/api-notes.md for what the schema offers and why queries look the way
they do.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)


class NexusError(RuntimeError):
    pass


class QuotaExhausted(NexusError):
    pass


def api_key() -> str | None:
    return os.environ.get(config.API_KEY_ENV) or None


def _headers() -> dict[str, str]:
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    key = api_key()
    if key:
        headers["apikey"] = key
    return headers


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), config.BACKOFF_MAX_S)
            except ValueError:
                pass
    return min(config.BACKOFF_BASE_S * 2**attempt, config.BACKOFF_MAX_S)


def _request_with_backoff(client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        response = None
        try:
            response = client.request(method, url, **kw)
            if response.status_code == 429 or response.status_code >= 500:
                delay = _retry_delay(attempt, response)
                log.warning("HTTP %s from %s; backing off %.0fs", response.status_code, url, delay)
                time.sleep(delay)
                continue
            return response
        except httpx.TransportError as exc:
            last_exc = exc
            delay = _retry_delay(attempt, None)
            log.warning("%s on %s; retrying in %.0fs", type(exc).__name__, url, delay)
            time.sleep(delay)
    raise NexusError(f"Giving up on {url} after {config.MAX_RETRIES} attempts") from last_exc


class GraphQLClient:
    def __init__(self, url: str = config.GRAPHQL_URL, min_interval: float | None = None):
        self.url = url
        self.min_interval = config.GRAPHQL_MIN_INTERVAL_S if min_interval is None else min_interval
        self._client = httpx.Client(headers=_headers(), timeout=config.HTTP_TIMEOUT_S)
        self._last = 0.0
        self.requests = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GraphQLClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        response = _request_with_backoff(
            self._client, "POST", self.url, json={"query": query, "variables": variables or {}}
        )
        self.requests += 1
        if response.status_code != 200:
            raise NexusError(f"GraphQL HTTP {response.status_code}: {response.text[:300]}")
        body = response.json()
        if body.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
            # Partial data is still useful (e.g. one deleted mod in a batch).
            if not body.get("data"):
                raise NexusError(f"GraphQL error: {msgs}")
            log.debug("GraphQL partial errors: %s", msgs)
        return body["data"]


class RestClient:
    """v1 REST. Needs an API key. Reads X-RL-* headers and refuses to dip into
    the reserve so the user's key stays usable for their mod manager."""

    def __init__(self, key: str | None = None):
        self.key = key or api_key()
        if not self.key:
            raise NexusError(f"v1 REST needs an API key in ${config.API_KEY_ENV}")
        headers = _headers() | {"apikey": self.key}
        self._client = httpx.Client(headers=headers, timeout=config.HTTP_TIMEOUT_S)
        self.hourly_remaining: int | None = None
        self.daily_remaining: int | None = None
        self.hourly_reset: str | None = None

    def close(self) -> None:
        self._client.close()

    def _check_quota(self) -> None:
        if self.daily_remaining is not None and self.daily_remaining <= config.V1_DAILY_RESERVE:
            raise QuotaExhausted(f"v1 daily quota at reserve ({self.daily_remaining} left)")
        if self.hourly_remaining is not None and self.hourly_remaining <= config.V1_HOURLY_RESERVE:
            raise QuotaExhausted(
                f"v1 hourly quota at reserve ({self.hourly_remaining} left, resets {self.hourly_reset})"
            )

    def get(self, path: str) -> Any:
        self._check_quota()
        response = _request_with_backoff(self._client, "GET", f"{config.REST_URL}{path}")
        h = response.headers
        if "X-RL-Hourly-Remaining" in h:
            self.hourly_remaining = int(h["X-RL-Hourly-Remaining"])
            self.daily_remaining = int(h.get("X-RL-Daily-Remaining", self.daily_remaining or 0))
            self.hourly_reset = h.get("X-RL-Hourly-Reset")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise NexusError(f"v1 HTTP {response.status_code} for {path}: {response.text[:200]}")
        return response.json()

    def mod(self, domain: str, mod_id: int) -> dict[str, Any] | None:
        return self.get(f"/games/{domain}/mods/{mod_id}.json")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

MOD_FIELDS = """
  modId uid name author category status adultContent endorsements downloads
  createdAt updatedAt modCategory { categoryId name }
"""

COLLECTIONS_QUERY = """
query($domain: String!, $offset: Int!, $count: Int!, $sort: [CollectionsSearchSort!]) {
  collectionsV2(
    filter: { gameDomain: [{ value: $domain }], collectionStatus: [{ value: "listed" }] }
    sort: $sort, offset: $offset, count: $count
  ) {
    totalCount
    nodes {
      id slug name endorsements totalDownloads uniqueDownloads
      overallRating overallRatingCount adultContent updatedAt lastPublishedAt
      category { name } user { name }
      latestPublishedRevision { revisionNumber modCount }
    }
  }
}
"""

REVISION_QUERY = """
query($slug: String!, $domain: String!, $revision: Int) {
  collectionRevision(slug: $slug, domainName: $domain, revision: $revision, viewAdultContent: true) {
    revisionNumber
    modFiles { optional file { mod { %s } } }
  }
}
""".replace("%s", MOD_FIELDS)

MODS_QUERY = """
query($ids: [CompositeIdInput!]!, $count: Int) {
  legacyMods(ids: $ids, count: $count) {
    nodes {
      %s
      modRequirements {
        nexusRequirements(count: 25) { nodes { modId modName gameId externalRequirement notes } }
        modsRequiringThisMod(count: 0) { totalCount }
      }
    }
  }
}
""".replace("%s", MOD_FIELDS)


def global_count_query(domain: str, uids: list[str]) -> str:
    """One request, many aliased reverse lookups: how many collections (for the
    whole game, not just what we crawled) contain each mod."""
    parts = [
        f'm{i}: collectionsV2(filter: {{ gameDomain: [{{ value: "{domain}" }}], '
        f'modUid: [{{ value: "{uid}" }}] }}, count: 0) {{ totalCount }}'
        for i, uid in enumerate(uids)
    ]
    return "query {\n" + "\n".join(parts) + "\n}"


def mod_uid(game_id: int, mod_id: int) -> str:
    """v2 mod uid = (gameId << 32) | modId (verified against the API)."""
    return str((game_id << 32) | mod_id)


SORTS = {
    "endorsements": [{"endorsements": {"direction": "DESC"}}],
    "downloads": [{"downloads": {"direction": "DESC"}}],
    "rating": [{"rating": {"direction": "DESC"}}],
    "recent": [{"updatedAt": {"direction": "DESC"}}],
}
