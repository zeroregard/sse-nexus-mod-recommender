# sse-nexus-mod-recommender (`modrec`)

Recommends Skyrim Special Edition mods you don't have yet, based on the mods you
do have, by mining Nexus Mods **collections** for co-occurrence.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported
> by Nexus Mods. It uses their public API politely: it sends an identifying
> User-Agent, throttles itself, backs off on HTTP 429, and caches everything.

## The idea

Curated collections are thousands of hand-built mod lists. If the mods you
have, A and B, keep turning up in collections that also contain C, then C is
probably something you'd like.

The catch: the naive version of that just recommends the most popular mods,
since everyone already has USSEP, SkyUI and SSE Engine Fixes. modrec tries hard
to find the *niche* mods that go with your *unusual* ones instead:

| Problem | What modrec does |
|---|---|
| Popular mods co-occur with everything | Uses **lift / PMI** (how much more often A and C appear together than chance predicts), not raw counts. |
| Your common mods say nothing about your taste | Weights each of your mods by **IDF**: log(all collections / collections containing it). USSEP in 90% of lists contributes about nothing; the odd mod only you and 12 curators use drives the results. It uses the *global* count across every Skyrim SE collection, which comes from the API's reverse lookup. |
| Big curated lists matter more than a 5-mod list | **Collection weight**: log(endorsements) × size factor × rating × recency. Tiny lists count less, and so do 2,000-mod megalists (in those, "A and C both appear" doesn't tell you much). |
| Forks and re-uploads count the same list many times | **Dedupe**: collections with Jaccard similarity > 0.8 collapse into the heaviest one. |
| Download count measures popularity, not quality | **Quality = endorsement rate**, endorsements per unique download, shrunk toward the median. A 3,000-download gem can beat a 300,000-download staple. |
| Frameworks and libraries show up everywhere | **Requirements graph**: a mod that one of your mods *requires* isn't recommended. It goes in a separate "you have X but not its requirement Y" section. Mods that hundreds of other mods require (libraries) get demoted. Candidates that need mods you don't have get a small penalty and list what they need. |
| Ten texture mods in a row | **Diversity**: a per-category cap plus MMR re-ranking. |

For each recommendation you get the mods of yours that drove it and the
collections where they co-occur, so you can judge whether to trust it.

```
score(C) = Σ_{A ∈ yours} assoc(A,C) · idf(A) · confidence(A,C)
           × quality(C) × recency(C) × library_penalty(C) × unmet_requirements_penalty(C)

assoc(A,C)      = max(0, log lift),   lift = p_w(A,C) / (p_w(A) p_w(C))
p_w(·)          = collection-weighted frequency over deduplicated collections
confidence(A,C) = n / (n + SHRINK_K), n = distinct collections containing both (≥ MIN_COOCCURRENCE)
```

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Linux is the
target platform.

```sh
git clone https://github.com/zeroregard/sse-nexus-mod-recommender
cd sse-nexus-mod-recommender
uv sync
uv run modrec --help
```

You don't need an API key. Everything uses the v2 GraphQL API, which allows
anonymous reads. If you set `NEXUS_API_KEY`, it's sent along. It also enables
the optional v1 REST lookups (`crawl --v1-budget N`), which fetch real
unique-download counts. Those lookups read the `X-RL-*` headers and stop well
before your quota runs out.

## Usage

```sh
# 1. Build the local index (resumable; Ctrl-C any time, re-run to continue).
#    300 collections ≈ 10-15 minutes, then it's all offline.
uv run modrec crawl --game skyrimse --limit 300

# 2. Detect your installed mods (Limo auto-detected; see adapters below).
uv run modrec scan                 # or: --adapter manual --path mods.txt

# 3. Recommendations.
uv run modrec recommend --top 25
uv run modrec recommend --explain                 # component breakdown + collections
uv run modrec recommend --category "Animation" -f markdown > recs.md
uv run modrec recommend -f json | jq '.recommendations[0]'

# 4. Everything about one result (works for filtered mods too, and says why).
uv run modrec why 12604

# 5. Is the scoring any good? See "Validation".
uv run modrec evaluate
uv run modrec evaluate --collections 60
```

The index lives in `~/.local/share/modrec/skyrimse.sqlite`. You can override
the path with `--db` or `MODREC_DB`.

`crawl` is incremental. It refreshes the collection listing (cheap), then
re-fetches a collection's mod list only when a new revision has been
published. Mod requirements are fetched once per mod. `--refresh` forces
everything to be fetched again, and `--limit 600` extends the index. `scan`
also fetches metadata, requirements and global collection counts for your
own mods, using `--offline` skips that.

## Mod manager adapters

| Adapter | Status | Reads |
|---|---|---|
| `limo` | ✅ | Limo's `Limo.conf` (native `~/.config/` or Flatpak `~/.var/app/io.github.limo_app.limo/config/`) → each staging dir's `lmm_mods.json` → `remote_mod_id` (the Nexus mod id; no filename guessing). The staging dir that manages Skyrim SE is picked by Steam app id / name / mod URLs. `--path` takes a staging dir directly. |
| `manual` | ✅ | A text file with one Nexus mod id or mod URL per line (`#` comments). Default `~/.config/modrec/mods.txt`. |
| `mo2` | stub | `mods/*/meta.ini` → `modid=`, `gameName=`. See `adapters/mo2.py`. |
| `vortex` | stub | `AppData/Roaming/Vortex/state.v2` LevelDB. See `adapters/vortex.py`. |

Without `--adapter`, modrec tries them in that order and uses the first one
that detects the game. To add a manager, implement the `ModManagerAdapter`
protocol in `src/modrec/adapters/base.py` (`detect(game)` and
`installed_mods(game)`) and register it in `adapters/__init__.py`.

## Validation

`modrec evaluate` holds out mods and checks whether they come back:

- **Your list** (default): hides `--holdout 10` of your mods, recommends from
  the rest, and counts how many hidden mods land in the top `--k 25`, over
  `--trials 5` trials. Only mods it could recover in principle are held out:
  mods that are in the index, published, and not required by another mod you
  have.
- **Leave-one-collection-out** (`--collections N`): treats N real collections
  as stand-in users. It removes each one, plus anything overlapping it by more
  than 50%, from the index before testing. You don't need a scan for this,
  which makes it the benchmark to tune against.

Both compare against two baselines: **popularity** (rank by weighted collection
frequency) and **raw co-occurrence** (sum of plain co-occurrence counts with
your mods). *Niche recall* only counts held-out mods at or below the median
collection frequency. That's the number that shows whether modrec does its
actual job.

Example, 300 collections indexed, `evaluate --collections 60`: *(see the
"Results" section below. It's regenerated when weights change.)*

## Tuning the weights

Every constant is in [`src/modrec/config.py`](src/modrec/config.py), grouped
and commented. You can try a change without editing anything:

```sh
uv run modrec --set SHRINK_K=5 --set MAX_PER_CATEGORY=3 evaluate --collections 60
uv run modrec --set ASSOCIATION=lift recommend --explain
```

Knobs worth trying first:

| Constant | Effect |
|---|---|
| `SHRINK_K`, `MIN_COOCCURRENCE`, `MIN_CANDIDATE_COLLECTIONS` | How much evidence a pair or candidate needs. Raising them gives safer, more mainstream picks. Lowering them gives more adventurous, noisier ones. |
| `ASSOCIATION` | `ppmi` (log lift, which dampens extreme ratios) or `lift` (lift − 1, which rewards rare pairs harder). |
| `IDF_USE_GLOBAL` | Use Nexus-wide collection counts for your mods' IDF, or only the crawled subset. |
| `COLLECTION_*` | How much endorsements, size, rating and age matter. |
| `QUALITY_EXP`, `QUALITY_PRIOR_STRENGTH` | How hard the endorsement rate reorders things, and how many downloads it takes before the rate is trusted. |
| `RECENCY_HALF_LIFE_Y`, `RECENCY_FLOOR` | How much stale mods are penalised. |
| `LIBRARY_REQUIRING_THRESHOLD`, `LIBRARY_PENALTY`, `MISSING_REQUIREMENT_PENALTY` | Requirements-graph demotions. |
| `MAX_PER_CATEGORY`, `MMR_LAMBDA` | Diversity. `MMR_LAMBDA = 1` turns MMR off. `recommend --no-diversity` disables both. |
| `DEDUPE_JACCARD` | Threshold for treating two collections as the same list. |

A good workflow: change one knob, run `evaluate --collections 60` (a few
seconds), keep the change if niche recall goes up without total recall
dropping, then look at `recommend --explain` to check that the results make
sense.

## Layout

```
src/modrec/
  config.py     every tunable constant + game table
  nexus.py      GraphQL + REST clients (throttle, backoff, quotas) and queries
  db.py         SQLite schema and upserts
  crawl.py      resumable crawl: collections → mod lists → requirements → global counts
  index.py      weighted, deduplicated collection × mod sparse matrix
  scoring.py    lift/idf/quality/recency/requirements scoring, MMR, explanations
  evaluate.py   hold-out validation + baselines
  output.py     table / json / markdown rendering
  adapters/     Limo, Manual, MO2 (stub), Vortex (stub)
docs/api-notes.md   what the Nexus API offers (introspection results)
```

Run tests with `uv run pytest`. Lint with `uv run ruff check`.

## License

GPL-2.0. See [LICENSE](LICENSE).
