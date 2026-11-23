# Architecture

```
backend/     FastAPI: routers → pipeline → services
frontend/    Vue 3 + Vite + TypeScript, Naive UI, Tailwind v4
models/      prebuilt artifacts (not in git): embedding, ISRC bridge DB, popularity table
data/        hitster.db: durable game data (not in git)
notebooks/   embedding training (Kaggle)
scripts/     offline builders from the MusicBrainz dump + embedding evaluation
```

## Backend layers

```
routers/    HTTP only: parse, call a pipeline/service function, format, map errors
pipeline/   the algorithm: no FastAPI, no HTTP, no global state; unit-tested
services/   integrations and state: Spotify, embedding, SQLite bridge, popularity,
            durable game store, in-memory lobby store
core/       config, logging, bearer-token helper
```

The rule that keeps this clean: **`pipeline/` never imports FastAPI and never touches the
lobby store.** Routers read state out of the store and pass plain data in, which is why
`build_deck` takes libraries and clusters as arguments rather than a lobby id, and why it can
be tested without a server.

| Pipeline module | Role |
|---|---|
| `models.py` | domain types: `SourcedTrack`, `PlayerLibrary`, `MatchedSong`, `PlayerClusters`, … |
| `library.py` | Spotify JSON → normalized tracks; pagination, dedupe, cache freshness policy |
| `matching.py` | ISRC set-intersection across players (pure; no embeddings) |
| `scoring.py` | `occurrence_weight`, the one scoring primitive |
| `clustering.py` | per-player taste clusters, built when a player finishes their profile |
| `deck.py` | the greedy tiered deck builder ([deck-builder.md](deck-builder.md)) |
| `track_resolution.py` | the single path from an embedding MBID to a playable Spotify track |
| `taste.py` | reports for the taste-analysis tool |

## Game flow

1. **Lobby** (`/start`). 2–5 players connect Spotify one at a time in the host's browser.
   OAuth is a full-page redirect, so the lobby is saved to `sessionStorage` and restored on
   return.
2. **Connect** → `POST /lobby/{id}/player`. Fetches the player's top tracks right away
   (short 100 / medium 500 / long 700, paginated), so they are ready by the time the player
   has picked playlists.
3. **Build profile** → `POST /lobby/{id}/player/{pid}/sources`. Fetches the chosen playlists
   (only ones the player owns, since Spotify refuses the rest), assembles the library, and
   **clusters the player's taste immediately** so Start Game doesn't pay for it. Libraries
   stay server-side; only summaries go back.
4. **Skip past games** → `GET /lobby/{id}/history`. Lists past sessions involving these players.
5. **Start game** → `POST /lobby/{id}/pool` builds the deck and `POST /session` opens a session.
   The deck goes to the game view through `sessionStorage`.
6. **Play.** One shared shuffled draw pile, turns rotate: listen, place the card in a
   timeline, reveal, award a correct guess. The app deals cards and keeps score; the players
   referee whether a placement was right. Audio plays through the Spotify Web Playback SDK
   from the first connected player's account (Premium only) and never autoplays. Every card
   dealt is recorded to the session as it happens (`POST /session/{id}/played`), so an
   abandoned game still counts.
7. **Play again** rebuilds against the same lobby, excluding what was played and carrying
   over what wasn't.

## State and persistence

| What | Where | Lifetime |
|---|---|---|
| Lobby: players, tokens, libraries, clusters | `lobby_store` (in memory) | 1 h idle; lost on restart |
| Extracted libraries | `data/hitster.db`, keyed by Spotify user id | top tracks: 7 / 30 / 180-day TTL per range; playlists: until their `snapshot_id` changes |
| Play history (sessions, players, songs played) | `data/hitster.db` | permanent |
| MBID → Spotify track cache | `models/isrc_bridge.db` | permanent, regenerable |
| Deck, scores, timelines | browser `sessionStorage` | the tab |

- **User data never goes in `models/`**, a directory of large artifacts you might delete and
  rebuild.
- **History is keyed on players, not groups.** Plays hang off a session, so per-player
  history is a join. If three people played last week, those songs are still skipped when a
  fourth joins.
- **Clusters are recomputed, not cached.** They are a cheap pure function of the library
  plus constants that were still being tuned.
- Tokens expire after about 1 h and no refresh tokens are stored (deliberately, for v1).

## The taste-analysis tool (`/analysis`)

A dev-only page for tuning the deck builder's embedding tiers on real accounts. Connect one or
two players (same flow as the lobby), then:

| Panel | Shows | Corresponds to |
|---|---|---|
| clusters | a library split into taste zones (k-means or agglomerative cut), each song with its similarity to the centroid | `clustering.py`, which is how `CLUSTER_CUT` / `CLUSTER_MIN_SIZE` were picked |
| neighbors | the most popular songs near each centroid | what the embedding "pulls" |
| proximity | every cross-player pair of clusters, by cosine similarity | the shared-taste signal |
| nearest-seeds | one player's real songs closest to another player's centroid | tier 2 (bridge) |
| combine | neighbours of a blend of two players' centroids | tier 3 (synthetic) |

Neighbour names come from MusicBrainz (`mbid_meta`). Spotify ISRC search alone resolved only
~19% of embedding MBIDs, because a MusicBrainz recording's ISRC often differs from the one on
Spotify's copy. Anything the tool resolves is cached, so browsing it warms the game's cache.

## Gotchas

- Spotify requires **`127.0.0.1`**, not `localhost`, in OAuth redirect URIs.
- In Spotify **Development Mode**, every player's account must be added under *User
  Management* in the dashboard, or their login fails.
- On a 429, the Spotify client retries once only if `Retry-After` ≤ 5 s. Otherwise it raises
  `SpotifyRateLimited`, and batch resolution switches to cache-only for the rest of the run
  instead of hammering the API.
- The bridge DB is ~850 MB: always use the batched lookups, never a per-song query in a loop.
- Docker's `node_modules` volume only populates when first created; after a dependency change,
  remove it (`docker volume rm hitster-maker_frontend_vue_node_modules`) and recreate.
