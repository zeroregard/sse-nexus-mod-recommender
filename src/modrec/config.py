"""Every tunable knob in one place.

Scoring weights live at the bottom. Change a constant, re-run `modrec recommend`
(or `modrec evaluate`) — nothing downstream of the crawl touches the network, so
iteration is instant.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Game:
    alias: str
    domain: str  # Nexus URL domain, e.g. nexusmods.com/<domain>/mods/123
    game_id: int  # numeric Nexus game id (used in GraphQL mod lookups and mod uids)
    steam_app_id: int | None = None
    names: tuple[str, ...] = ()  # names mod managers use for this game


GAMES: dict[str, Game] = {
    "skyrimse": Game(
        alias="skyrimse",
        domain="skyrimspecialedition",
        game_id=1704,
        steam_app_id=489830,
        names=("Skyrim Special Edition", "Skyrim SE", "SkyrimSE", "Skyrim Anniversary Edition"),
    ),
}
DEFAULT_GAME = "skyrimse"


def get_game(name: str) -> Game:
    key = name.lower()
    if key in GAMES:
        return GAMES[key]
    for game in GAMES.values():
        if key == game.domain:
            return game
    raise KeyError(f"Unknown game {name!r}; known: {', '.join(GAMES)}")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "modrec"


def default_db_path(game: Game) -> Path:
    override = os.environ.get("MODREC_DB")
    if override:
        return Path(override)
    return data_dir() / f"{game.alias}.sqlite"


# ---------------------------------------------------------------------------
# Nexus API client
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
REST_URL = "https://api.nexusmods.com/v1"
USER_AGENT = "modrec/0.1 (+https://github.com/zeroregard/sse-nexus-mod-recommender)"
API_KEY_ENV = "NEXUS_API_KEY"

# v2 sends no rate-limit headers, so self-throttle conservatively.
GRAPHQL_MIN_INTERVAL_S = 0.5
HTTP_TIMEOUT_S = 90.0
MAX_RETRIES = 6
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 120.0

# v1 REST: stop spending quota when fewer than this many requests remain.
V1_HOURLY_RESERVE = 100
V1_DAILY_RESERVE = 1000

# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

COLLECTION_PAGE_SIZE = 50
MOD_BATCH_SIZE = 40  # mods per legacyMods() request (complexity ≈ n × 25 reqs × 6 ≤ 10k)
GLOBAL_COUNT_BATCH_SIZE = 25  # aliased collectionsV2 count queries per request
MIN_COLLECTION_SIZE = 5  # don't bother fetching collections smaller than this
DEFAULT_CRAWL_LIMIT = 300

# ---------------------------------------------------------------------------
# Scoring
#
#   score(C) = Σ_{A ∈ installed} assoc(A,C) · idf(A) · confidence(A,C)
#              × quality(C) × recency(C) × penalties(C)
#
# where assoc/confidence are computed from *collection-weighted* co-occurrence
# over the deduplicated collection set.
# ---------------------------------------------------------------------------

# --- collection weight ------------------------------------------------------
# weight(col) = log1p(endorsements)^ENDORSE_EXP
#             × size_factor × rating_factor × recency_factor
COLLECTION_ENDORSE_EXP = 1.0
# Collections smaller than this get linearly less weight (a 5-mod list = 5/20).
COLLECTION_FULL_WEIGHT_SIZE = 20
# Collections larger than this get (REF/size)^LARGE_EXP: in a 2,000-mod
# megalist, "A and C both appear" is weak evidence that they belong together.
COLLECTION_LARGE_REF_SIZE = 600
COLLECTION_LARGE_EXP = 0.5
COLLECTION_RATING_EXP = 0.5  # rating is 0-100; factor = (rating/100)^EXP
COLLECTION_DEFAULT_RATING = 60.0  # used when a collection has no rating yet
COLLECTION_RECENCY_HALF_LIFE_Y = 2.0
COLLECTION_RECENCY_FLOOR = 0.4

# --- dedupe -----------------------------------------------------------------
# Collections whose mod sets overlap more than this (Jaccard) collapse into
# one, keeping the heaviest; forks/re-uploads otherwise get counted repeatedly.
DEDUPE_JACCARD = 0.8

# --- association ------------------------------------------------------------
# "ppmi": max(0, log(lift))   "lift": max(0, lift - 1)
ASSOCIATION = "ppmi"
# Ignore pairs that co-occur in fewer distinct (deduped) collections than this.
MIN_COOCCURRENCE = 2
# Shrink small-sample associations: confidence = n / (n + SHRINK_K).
SHRINK_K = 3.0
# A candidate must appear in at least this many deduped collections.
MIN_CANDIDATE_COLLECTIONS = 3

# Your mods whose collection sets overlap at least this much (Jaccard) form a
# "driver group" that contributes once (its strongest member), not once per
# mod. Stops a 40-mod series that always travels together from dominating.
DRIVER_GROUP_JACCARD = 0.5

# --- idf over the user's mods -----------------------------------------------
# idf(A) = log((N + 1) / (df(A) + 1)). Uses the *global* collection count for A
# (from the Nexus reverse lookup) when available, else the local crawl.
IDF_USE_GLOBAL = True
IDF_FLOOR = 0.0

# --- quality: endorsement *rate* -------------------------------------------
# rate = (endorsements + PRIOR_STRENGTH·prior) / (unique_dls + PRIOR_STRENGTH)
# prior = median rate over the index. quality = clip((rate/prior)^EXP).
QUALITY_PRIOR_STRENGTH = 2000.0
QUALITY_EXP = 0.5
QUALITY_MIN = 0.4
QUALITY_MAX = 2.5
# v2 only exposes total downloads on Mod; unique ≈ total × this when the v1
# unique figure isn't cached. Only matters when mixing the two sources.
UNIQUE_TO_TOTAL_RATIO = 0.45

# --- recency (of the candidate mod's last update) --------------------------
RECENCY_HALF_LIFE_Y = 4.0
RECENCY_FLOOR = 0.6

# --- requirements graph -----------------------------------------------------
# A candidate that something you already have requires is plumbing, not a
# recommendation: it's moved to the "missing requirements" section instead.
FILTER_REQUIRED_BY_INSTALLED = True
# Candidates required by many mods across Nexus are libraries/frameworks.
LIBRARY_REQUIRING_THRESHOLD = 150
LIBRARY_PENALTY = 0.5
# Per unmet Nexus requirement of the candidate (you'd have to install more).
MISSING_REQUIREMENT_PENALTY = 0.9
MISSING_REQUIREMENT_MIN_FACTOR = 0.5

# --- diversity --------------------------------------------------------------
MAX_PER_CATEGORY = 4
# MMR trade-off: 1.0 = pure relevance, lower = more diverse. Similarity is the
# cosine of the two mods' weighted collection vectors.
MMR_LAMBDA = 0.85
MMR_POOL = 200

# --- explanation ------------------------------------------------------------
EXPLAIN_TOP_DRIVERS = 5
EXPLAIN_COLLECTIONS_PER_DRIVER = 3

# --- content filters --------------------------------------------------------
INCLUDE_ADULT = True


def overridden(**overrides):
    """A config snapshot with some constants replaced (for experiments)."""
    from types import SimpleNamespace

    values = {k: v for k, v in globals().items() if k.isupper()}
    unknown = set(overrides) - set(values)
    if unknown:
        raise KeyError(f"Unknown config constant(s): {', '.join(sorted(unknown))}")
    return SimpleNamespace(**(values | overrides))


def apply_override(assignment: str) -> tuple[str, object]:
    """Parse `NAME=value` and set it on this module, keeping the original type."""
    name, _, raw = assignment.partition("=")
    name = name.strip().upper()
    g = globals()
    if name not in g or not name.isupper():
        raise KeyError(f"Unknown config constant {name!r}")
    current = g[name]
    if isinstance(current, bool):
        value: object = raw.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(current, (int, float)) and not isinstance(current, bool):
        value = (
            type(current)(float(raw)) if isinstance(current, int) and "." not in raw else float(raw)
        )
    else:
        value = raw.strip()
    g[name] = value
    return name, value
