# Nexus Mods API notes

Findings from introspecting and probing the API on 2026-09-24. Re-run
introspection with the snippet at the bottom if something stops working.

## Endpoints

| API | URL | Auth | Limits |
|---|---|---|---|
| v2 GraphQL | `POST https://api.nexusmods.com/v2/graphql` (`graphql.nexusmods.com` serves the explorer) | None needed for everything used here; `apikey` header accepted | No `X-RL-*` headers on responses. Each query has a **complexity limit of 10,000** (see below). modrec throttles itself to 2 req/s. |
| v1 REST | `https://api.nexusmods.com/v1/...` | **Required**: `apikey` header, otherwise `401` | Per-key hourly and daily quotas (roughly 2,000/hour, 20,000/day; not verified here since no key was available), reported in `X-RL-Hourly-Remaining`, `X-RL-Daily-Remaining`, `X-RL-Hourly-Reset`. |

Anonymous v2 requests see adult-flagged collections and mods (the listing
returns them; `collectionRevision` needs `viewAdultContent: true`).

## 1. Listing collections for a game — yes

```graphql
collectionsV2(
  filter: { gameDomain: [{ value: "skyrimspecialedition" }], collectionStatus: [{ value: "listed" }] }
  sort: [{ endorsements: { direction: DESC } }]   # also downloads, rating, recentRating, createdAt, updatedAt
  offset: 0, count: 50
) { totalCount nodes { id slug name ... } }
```

- Offset pagination. `count: 100` works. `totalCount` for Skyrim SE: **4,758**.
- Also filterable by `categoryName`, `tag`, `gameVersion`, `adultContent`,
  `hasPublishedRevision`, `recentRating`, `userId`, and (see below) `modUid`.
- A collection's mod list lives on a *revision*:
  `collectionRevision(slug, domainName, revision, viewAdultContent) { modFiles { optional file { mod { ... } } } }`.
  `modFiles` is the full list in one response (a 2,444-mod collection took
  ~3.5s) and — usefully — nests the full `Mod` object, so one request per
  collection gives us both membership and per-mod metadata.
- `file` or `file.mod` can be `null` when a file/mod has since been removed.

## 2. Reverse lookup (mod → collections) — yes, via a filter

There's no dedicated field, but `CollectionsSearchFilter` has `modUid`:

```graphql
collectionsV2(filter: { gameDomain: [{ value: "skyrimspecialedition" }],
                        modUid: [{ value: "7318624284988" }] }, count: 0) { totalCount }
```

- SkyUI (`modUid 7318624284988`) → 3,597 of 4,758 collections.
- `mod uid = (gameId << 32) | modId` — Skyrim SE `gameId` is 1704, so USSEP
  (mod 266) is `7318624272650`. Confirmed against `Mod.uid`.
- Many lookups batch into one request with aliases (`m0: collectionsV2(...)
  m1: ...`); 25 per request is well within the complexity budget.

**How modrec uses it:** not to build the co-occurrence index (we still need
full mod lists, so crawling forward is simpler and gives everything in one
pass), but to get the *global* collection frequency of each of the user's
mods — the exact denominator for `idf(A)` over all 4,758 collections, not
just the few hundred we crawled.

## 3. Per-collection metadata

On `Collection`: `endorsements`, `totalDownloads`, `uniqueDownloads`,
`overallRating` (a **string** percentage, e.g. `"75.3"`),
`overallRatingCount`, `recentRating`/`recentRatingCount`, `createdAt`,
`updatedAt`, `firstPublishedAt`, `lastPublishedAt`, `adultContent`,
`category { name }` (e.g. "Themed", "Total Overhaul"), `tags`, `user { name }`,
`latestPublishedRevision { revisionNumber modCount rating { average positive total } }`.

`updatedAt` changes constantly (it bumps on downloads/comments); use
`lastPublishedAt` or the revision number to detect real changes. modrec
re-fetches a collection's mods only when `latestPublishedRevision.revisionNumber`
differs from the cached one.

## 4. Per-mod metadata

On `Mod`: `modId`, `uid`, `name`, `author`, `uploader { name }`, `category`
(string) and `modCategory { categoryId name }`, `endorsements`, `downloads`,
`createdAt`, `updatedAt`, `status` (`published`, `hidden`, ...), `adultContent`,
`version`, `summary`, `tags`.

- **Unique downloads are not on the v2 `Mod` type.** `ModsSort` has a
  `uniqueDownloads` sort key but no field, and `Mod.downloads` is total
  downloads (sorting by `uniqueDownloads` orders SkyUI above USSEP although
  USSEP has more `downloads`; mod 78519 has `downloads` 211,621 while its
  current files total 109k total / 65k unique). v1's
  `GET /v1/games/{domain}/mods/{id}.json` has `mod_unique_downloads`.
  modrec uses `downloads × UNIQUE_TO_TOTAL_RATIO` unless the v1 figure has been
  fetched (`crawl --v1-budget N`, needs a key).
- `ModFile` does have `uniqueDownloads`/`totalDownloads` per file.
- Batch lookup: `legacyMods(ids: [{gameId: 1704, modId: 266}, ...], count: N)`.
  Missing/deleted ids are silently dropped from `nodes` (82 of ids 1000–1099
  exist). `modsByUid(uids: [...])` is the uid-based equivalent.

### Requirements

```graphql
modRequirements {
  nexusRequirements(count: 25) { nodes { modId modName gameId externalRequirement notes url } }
  modsRequiringThisMod(count: 0) { totalCount }
  dlcRequirements { gameExpansion { name } notes }
}
```

Both directions are available. `modsRequiringThisMod.totalCount` is a good
"is this a library/framework" signal (SkyUI: 2,300; USSEP: 2,503). External
requirements (non-Nexus URLs) have `externalRequirement: true`.

## Query complexity limit

Errors come back as HTTP 200 with
`{"errors":[{"message":"Query has complexity of 30200, which exceeds max complexity of 10000","extensions":{"code":"QUERY_TOO_COMPLEX"}}]}`
and no data. Paginated sub-fields are costed by their `count` argument:
`legacyMods` of 50 mods × `nexusRequirements(count: 100)` ≈ 30,200, so modrec
uses 40 mods × 25 requirements. `collectionRevision.modFiles` is not
paginated and even 2,000-mod collections pass.

## Introspection snippet

```python
import httpx, json
Q = """{ __schema { types { name kind fields { name type { name kind ofType { name kind } } } } } }"""
r = httpx.post("https://api.nexusmods.com/v2/graphql", json={"query": Q},
               headers={"User-Agent": "modrec-introspect"})
json.dump(r.json(), open("schema.json", "w"), indent=1)
```
