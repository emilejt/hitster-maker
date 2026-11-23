"""The greedy, tiered game-deck builder (full write-up: docs/deck-builder.md).

Every song added to the deck is the current top scorer of

    score(s) = A·coverage + B·evenness + C·band + D·gapClosing

where coverage/evenness/band are static per candidate (cached) and only the gap term is
recomputed each iteration against the running per-player contribution totals:

    coverage   = (owners − 1) / (players − 1)
    evenness   = 1 − (maxW − minW)                 # W = the song's per-owner weights
    band       = exp(−((meanW − BAND_CENTER)/BAND_WIDTH)²)
    gapClosing = gap_before − gap_after            # gap = max(contrib) − min(contrib)

The builder adds while below a soft size, then — once fair-past-soft — stops; while unfair
past soft it *swaps* the leader's least-valuable deck song for a better candidate (only if the
net Δscore is positive). When no unlocked candidate clears the FLOOR, it **widens** to the next
candidate tier (never chooses between fallbacks — only widens):

    Tier 1  real matches (songs shared by ≥2 players)
    Tier 2  bridge songs — the lagging player's solo songs, ranked by proximity to another
            player's real cluster; credits the lagging player their measured weight and the
            bridged player a discounted, proximity-scaled estimate.
    Tier 3  synthetic — popular unowned songs near a shared centroid region; all contributions
            estimated, hard-capped so no player's tally is >SYNTHETIC_CAP synthetic.
            Currently disabled (TIER3_ENABLED).

Every add/swap logs a trace row; a summary logs the final split — the surface for tuning the
scalars below. Pure (framework-agnostic); clusters are handed in as plain data by the router.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from app.core.logging import get_logger
from app.pipeline import matching, track_resolution
from app.pipeline.models import (
    PlayerClusters,
    PlayerLibrary,
    Source,
    SourcedTrack,
)
from app.pipeline.scoring import FALLBACK_WEIGHT, occurrence_weight
from app.services import bridge_service, embedding_service, popularity_service

log = get_logger("pipeline.deck")

# ── Scoring coefficients + thresholds (TUNABLE — edit + reload) ────────────────
A_COVERAGE = 1.0
B_EVENNESS = 1.0
C_BAND = 0.7
D_GAP = 3.0
BAND_CENTER = 0.6
BAND_WIDTH = 0.2

SOFT_SIZE_PER_PLAYER = 15
# FLOOR / TOLERANCE are first-pass placeholders — tune them by reading the build trace.
FLOOR = 0.5  # min score to keep pulling (what makes the soft size "soft")
TOLERANCE = 1.0  # max acceptable max−min contribution gap that counts as "fair"

# Play-history exclusion can starve tier 1 — a trio burns 15x3 = 45 real matches per game, and
# those come only from the songs those specific people share. Rather than let that quietly
# produce short, lopsided decks, the builder retries with the OLDEST session's exclusions
# dropped whenever a build lands below this fraction of the soft size. Tiers 1-2 are pure
# in-memory work, so a rebuild is nearly free.
RELAX_FILL_FRACTION = 0.8

BRIDGE_DISCOUNT = 0.5  # tier-2 bridged-player estimate = proximity × this

# Tier 3 is built but switched off. Tiers 1/2 always have at least one *measured* owner; a
# tier-3 song is in nobody's library, so its worst case is a card no one at the table knows.
# It stays off until we've measured how often synthetic picks are actually recognized and
# made its unlock condition stricter. See docs/deck-builder.md.
TIER3_ENABLED = False
SYNTHETIC_CAP = (
    0.30  # no player's running tally may be more than this fraction synthetic
)

# ── Tier-2 / tier-3 generation knobs ──────────────────────────────────────────
_COMMON_GROUND_MIN_SIM = 0.3  # two players' centroids "share taste" above this cosine
_TIER3_CANDIDATES_PER_VECTOR = 200
# We rank the neighbour pool by our own popularity score and only *resolve* (hit Spotify for)
# the ones clearing this threshold — the point of the popularity pass is to avoid resolving all
# of them. _TIER3_MAX is then a safety cap on that already-filtered, cache-cold burst.
_TIER3_POP_THRESHOLD = 0.7
_TIER3_MAX = 200
_TIER3_MIN_PROX = 0.2  # a player must be this close to a synthetic song to be credited
_MAX_ITERS = 20000  # hard safety bound on the greedy loop


@dataclass
class Candidate:
    """One song that could enter the deck, with its cached static terms."""

    key: str  # isrc (tier 1/2) or "syn:<mbid>" (tier 3) — dedupe key
    track: SourcedTrack  # playable
    owners: dict[str, float]  # player_id → per-song weight (measured or estimated)
    tier: int
    coverage: float = 0.0
    evenness: float = 0.0
    band: float = 0.0
    static_score: float = 0.0
    # tier-3 only: per-owner portion of the weight that counts against the synthetic cap
    synthetic_owed: dict[str, float] = field(default_factory=dict)


# ── Static-term math ──────────────────────────────────────────────────────────
def _static_terms(
    weights: list[float], n_players: int
) -> tuple[float, float, float, float]:
    n = len(weights)
    coverage = (n - 1) / (n_players - 1) if n_players > 1 else 0.0
    evenness = 1.0 - (max(weights) - min(weights))
    mean = sum(weights) / n
    band = math.exp(-(((mean - BAND_CENTER) / BAND_WIDTH) ** 2))
    static = A_COVERAGE * coverage + B_EVENNESS * evenness + C_BAND * band
    return coverage, evenness, band, static


def _make_candidate(
    key: str,
    track: SourcedTrack,
    owners: dict[str, float],
    tier: int,
    n_players: int,
    synthetic_owed: dict[str, float] | None = None,
) -> Candidate:
    coverage, evenness, band, static = _static_terms(list(owners.values()), n_players)
    return Candidate(
        key=key,
        track=track,
        owners=owners,
        tier=tier,
        coverage=coverage,
        evenness=evenness,
        band=band,
        static_score=static,
        synthetic_owed=synthetic_owed or {},
    )


# ── Common-ground blend ───────────────────────────────────────────────────────
def _common_ground_vectors(centroids: dict[str, list[np.ndarray]]) -> list[np.ndarray]:
    """Blend (mean, renormalized) each pair of centroids from *different* players that are
    close, strongest overlaps first — the shared-taste query vectors for synthetic songs."""
    players = list(centroids)
    blends: list[tuple[float, np.ndarray]] = []
    for i in range(len(players)):
        for j in range(i + 1, len(players)):
            for c1 in centroids[players[i]]:
                for c2 in centroids[players[j]]:
                    sim = float(c1 @ c2)
                    if sim < _COMMON_GROUND_MIN_SIM:
                        continue
                    blended = c1 + c2
                    norm = float(np.linalg.norm(blended))
                    if norm > 0:
                        blends.append((sim, blended / norm))
    blends.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in blends]


# ── The builder ───────────────────────────────────────────────────────────────
class _DeckBuilder:
    def __init__(
        self,
        libraries: list[PlayerLibrary],
        clusters_by_player: dict[str, PlayerClusters],
        resolve_token: str | None,
        exclude_spotify_ids: set[str] | None = None,
        seed_spotify_ids: set[str] | None = None,
        exclude_isrcs: set[str] | None = None,
    ):
        self.libraries = libraries
        self.players = [lib.player_id for lib in libraries]
        self.lib_by_player = {lib.player_id: lib for lib in libraries}
        self.clusters_by_player = clusters_by_player
        self.token = resolve_token
        # "Play again" support: songs already played in a prior game are excluded from every tier
        # (never candidates); the prior deck's *unplayed* songs are pre-seeded so the new deck
        # starts with them and the greedy loop only tops it back up to full size.
        self.exclude_ids = exclude_spotify_ids or set()
        # ISRC is the better exclusion key — the same recording carries different Spotify ids
        # across markets and re-releases, so a spotify_id-only check lets repeats slip back in.
        # Songs with no ISRC still fall back to the id.
        self.exclude_isrcs = exclude_isrcs or set()
        self.seed_ids = seed_spotify_ids or set()

        self.contrib: dict[str, float] = {p: 0.0 for p in self.players}
        self.synthetic_contrib: dict[str, float] = {p: 0.0 for p in self.players}
        self.deck: list[Candidate] = []
        self.used: set[str] = set()
        self.unlocked = 1
        self.soft = SOFT_SIZE_PER_PLAYER * len(self.players)
        self.iterations = 0
        self.exit_reason = "unknown"

        # Tier 1 up front; tiers 2/3 are built lazily when unlocked.
        self.matches = matching.detect_matches(libraries)
        self.tier1 = self._build_tier1()
        self.tier2_by_player: dict[str, list[Candidate]] | None = None
        self.tier3: list[Candidate] | None = None

    def _excluded(self, track: SourcedTrack, isrc: str | None = None) -> bool:
        """Has this song already been played (this game, or a past session we're excluding)?"""
        if track.spotify_id in self.exclude_ids:
            return True
        candidate_isrc = isrc if isrc is not None else track.isrc
        return bool(candidate_isrc) and candidate_isrc in self.exclude_isrcs

    # ---- Tier 1: real matches ----
    def _build_tier1(self) -> list[Candidate]:
        n = len(self.players)
        out: list[Candidate] = []
        for isrc, m in self.matches.items():
            if len(m.owners) < 2:
                continue
            if self._excluded(m.track, isrc):
                continue  # already played (this game or an excluded past session)
            owners = {pid: c.weight for pid, c in m.owners.items()}
            out.append(_make_candidate(isrc, m.track, owners, tier=1, n_players=n))
        return out

    # ---- Tier 2: bridge (lagging player's solo songs near another player's cluster) ----
    def _ensure_tier2(self) -> None:
        if self.tier2_by_player is not None:
            return
        n = len(self.players)
        solo_by_player: dict[str, list[tuple[str, SourcedTrack]]] = {
            p: [] for p in self.players
        }
        for isrc, m in self.matches.items():
            if len(m.owners) == 1:
                owner = next(iter(m.owners))
                solo_by_player.setdefault(owner, []).append((isrc, m.track))

        # One batched bridge lookup for every solo song across all players, rather than a
        # fresh connection per song.
        mbid_by_isrc = bridge_service.get_mbids_for_isrcs(
            [isrc for solos in solo_by_player.values() for isrc, _ in solos if isrc]
        )

        self.tier2_by_player = {}
        for player, solos in solo_by_player.items():
            # Resolve each solo song to an in-vocab MBID.
            entries: list[tuple[str, str, SourcedTrack]] = []  # (isrc, mbid, track)
            for isrc, track in solos:
                if self._excluded(track, isrc):
                    continue  # already played (this game or an excluded past session)
                mbid = mbid_by_isrc.get(isrc) if isrc else None
                if mbid and embedding_service.is_in_model(mbid):
                    entries.append((isrc, mbid, track))
            if not entries:
                self.tier2_by_player[player] = []
                continue

            mbids = [mbid for _, mbid, _ in entries]
            # Best proximity (and which player it bridges to) over OTHER players' centroids.
            best: dict[str, tuple[float, str | None]] = {
                mbid: (-1.0, None) for mbid in mbids
            }
            for other in self.players:
                if other == player:
                    continue
                pc = self.clusters_by_player.get(other)
                if not pc:
                    continue
                for cluster in pc.clusters:
                    sims = embedding_service.cosine_mbids_against_vector(
                        mbids, cluster.centroid
                    )
                    for mbid, sim in sims.items():
                        if sim > best[mbid][0]:
                            best[mbid] = (sim, other)

            ranked: list[tuple[float, Candidate]] = []
            for isrc, mbid, track in entries:
                prox, bridged = best[mbid]
                if bridged is None or prox <= 0:
                    continue
                owners = {
                    player: occurrence_weight(track.source, track.rank),
                    bridged: prox * BRIDGE_DISCOUNT,
                }
                ranked.append(
                    (prox, _make_candidate(isrc, track, owners, tier=2, n_players=n))
                )
            ranked.sort(key=lambda x: x[0], reverse=True)
            self.tier2_by_player[player] = [c for _, c in ranked]

    # ---- Tier 3: synthetic songs near a shared centroid region ----
    async def _ensure_tier3(self) -> None:
        if self.tier3 is not None:
            return
        self.tier3 = []
        n = len(self.players)
        centroids = {
            p: [
                c.centroid
                for c in (
                    self.clusters_by_player.get(p).clusters
                    if self.clusters_by_player.get(p)
                    else []
                )
            ]
            for p in self.players
        }
        vectors = _common_ground_vectors(centroids)
        if not vectors:  # no close cross-player pairs — fall back to every centroid
            vectors = [c for cs in centroids.values() for c in cs]
        if not vectors:
            return

        excluded = self._excluded_mbids_for_tier3()
        cand_mbids: dict[str, None] = {}  # ordered set
        for v in vectors:
            for mbid in embedding_service.get_neighbors_for_vector(
                v, n=_TIER3_CANDIDATES_PER_VECTOR, exclude=excluded
            ):
                cand_mbids.setdefault(mbid, None)
        if not cand_mbids:
            return

        # Rank by popularity and keep only the songs clearing the threshold — those are the only
        # ones we resolve via Spotify (the rest never cost a call).
        pop = popularity_service.get_popularity_scores(list(cand_mbids))
        ranked_mbids = sorted(
            (m for m in cand_mbids if pop.get(m, 0.0) >= _TIER3_POP_THRESHOLD),
            key=lambda m: pop.get(m, 0.0),
            reverse=True,
        )[:_TIER3_MAX]
        if not ranked_mbids:
            log.info(
                "deck: tier 3 found no neighbour above popularity %.2f",
                _TIER3_POP_THRESHOLD,
            )
            return

        # Per-player proximity of each candidate to that player's nearest centroid → estimate.
        prox_by_player: dict[str, dict[str, float]] = {}
        for p in self.players:
            acc: dict[str, float] = {}
            for centroid in centroids.get(p, []):
                for mbid, sim in embedding_service.cosine_mbids_against_vector(
                    ranked_mbids, centroid
                ).items():
                    if sim > acc.get(mbid, -1.0):
                        acc[mbid] = sim
            prox_by_player[p] = acc

        resolved = await track_resolution.resolve_batch(ranked_mbids, self.token)
        for mbid in ranked_mbids:
            entry = resolved.get(mbid)
            if entry is None:
                continue
            track = track_resolution.to_sourced_track(entry, Source.FALLBACK)
            if track is None:
                continue
            if self._excluded(track):
                continue  # already played (this game or an excluded past session)
            owners = {
                p: FALLBACK_WEIGHT * prox_by_player[p].get(mbid, 0.0)
                for p in self.players
                if prox_by_player[p].get(mbid, 0.0) >= _TIER3_MIN_PROX
            }
            if not owners:
                continue
            self.tier3.append(
                _make_candidate(
                    f"syn:{mbid}",
                    track,
                    owners,
                    tier=3,
                    n_players=n,
                    synthetic_owed=dict(owners),
                )
            )

    def _excluded_mbids_for_tier3(self) -> set[str]:
        """MBIDs the synthetic search must not surface: songs already in the deck plus the
        existing tier-1/tier-2 candidates. Songs a player owns are deliberately *not* excluded:
        a shared-taste centroid landing on one is fine, only exact duplicates are dropped."""
        pools: list[Candidate] = list(self.tier1) + list(self.deck)
        if self.tier2_by_player:
            for cands in self.tier2_by_player.values():
                pools.extend(cands)
        # Tier-3 keys carry their MBID; tier-1/2 keys are ISRCs, resolved in one batched lookup.
        excluded = {c.key.removeprefix("syn:") for c in pools if c.tier == 3}
        isrcs = [c.key for c in pools if c.tier != 3]
        excluded.update(bridge_service.get_mbids_for_isrcs(isrcs).values())
        return excluded

    # ---- Scoring ----
    @staticmethod
    def _gap(contrib: dict[str, float]) -> float:
        return max(contrib.values()) - min(contrib.values())

    def _score_against(self, c: Candidate, contrib: dict[str, float]) -> float:
        before = self._gap(contrib)
        after = dict(contrib)
        for p, w in c.owners.items():
            after[p] += w
        return c.static_score + D_GAP * (before - self._gap(after))

    def _score(self, c: Candidate) -> float:
        return self._score_against(c, self.contrib)

    def _cap_ok(self, c: Candidate) -> bool:
        """Tier-3 hard cap: adding c must not push any owner's synthetic share over the cap."""
        if c.tier != 3:
            return True
        for p, owed in c.synthetic_owed.items():
            new_total = self.contrib[p] + c.owners[p]
            if (
                new_total > 0
                and (self.synthetic_contrib[p] + owed) / new_total > SYNTHETIC_CAP
            ):
                return False
        return True

    # ---- Candidate pool for the currently-unlocked tiers ----
    def _candidates(self) -> list[Candidate]:
        cands: list[Candidate] = list(self.tier1)
        if self.unlocked >= 2 and self.tier2_by_player is not None:
            lagging = min(self.contrib, key=lambda p: self.contrib[p])
            cands += self.tier2_by_player.get(lagging, [])
        if self.unlocked >= 3 and self.tier3 is not None:
            cands += self.tier3
        return [c for c in cands if c.key not in self.used]

    async def _widen(self) -> bool:
        if self.unlocked == 1:
            self.unlocked = 2
            self._ensure_tier2()
            log.info("deck: widened to tier 2 (bridge)")
            return True
        if self.unlocked == 2 and TIER3_ENABLED:
            self.unlocked = 3
            await self._ensure_tier3()
            log.info(
                "deck: widened to tier 3 (synthetic, %d candidates)",
                len(self.tier3 or []),
            )
            return True
        return False

    # ---- Mutations ----
    def _commit_add(self, c: Candidate, action: str) -> None:
        for p, w in c.owners.items():
            self.contrib[p] += w
        if c.tier == 3:
            for p, owed in c.synthetic_owed.items():
                self.synthetic_contrib[p] += owed
        self.deck.append(c)
        self.used.add(c.key)
        self._log_row(action, c)

    def _commit_remove(self, c: Candidate) -> None:
        for p, w in c.owners.items():
            self.contrib[p] -= w
        if c.tier == 3:
            for p, owed in c.synthetic_owed.items():
                self.synthetic_contrib[p] -= owed
        self.deck.remove(c)  # key stays in `used` so it can't immediately return

    # ---- Play-again seeding ----
    def _seed_deck(self) -> None:
        """Pre-commit the prior deck's unplayed songs so a new game starts with them.

        Only the deterministic tiers (1 = matches, 2 = bridge) are searched — they rebuild
        identically from the same libraries/clusters, so a carried-over song is found with its
        real weights. Synthetic (tier 3) isn't reproducible and is simply re-topped-up by the
        greedy loop. After seeding, `build()` fills the rest to the soft size as usual."""
        if not self.seed_ids:
            return
        self._ensure_tier2()
        pool: list[Candidate] = list(self.tier1)
        for cands in (self.tier2_by_player or {}).values():
            pool.extend(cands)
        seeded = 0
        for c in pool:
            if c.key in self.used or c.track.spotify_id not in self.seed_ids:
                continue
            self._commit_add(c, "seed")
            seeded += 1
        log.info("deck: seeded %d unplayed carry-over songs", seeded)

    # ---- The greedy loop ----
    async def build(self) -> list[Candidate]:
        self._seed_deck()
        while self.iterations < _MAX_ITERS:
            self.iterations += 1
            cands = self._candidates()
            if not cands:
                if await self._widen():
                    continue
                self.exit_reason = "tiers exhausted"
                break

            best = max(cands, key=lambda c: (self._score(c), c.static_score, c.key))

            # Tier-3 cap: skip (permanently) a synthetic song that would breach the cap.
            if not self._cap_ok(best):
                self.used.add(best.key)
                continue

            if self._score(best) < FLOOR:
                if await self._widen():
                    continue
                self.exit_reason = "no candidate above floor"
                break

            if len(self.deck) < self.soft:
                self._commit_add(best, "add")
                continue

            # At/above soft size.
            if self._gap(self.contrib) <= TOLERANCE:
                self.exit_reason = "fair past soft size"
                break

            # Unfair past soft size → try to swap out the leader's least-valuable deck song.
            leader = max(self.contrib, key=lambda p: self.contrib[p])
            victims = [d for d in self.deck if leader in d.owners]
            if not victims:
                if await self._widen():
                    continue
                self.exit_reason = "unfair but leader has no removable song"
                break
            victim = min(victims, key=lambda d: (d.static_score, d.key))

            contrib_wo = dict(self.contrib)
            for p, w in victim.owners.items():
                contrib_wo[p] -= w
            net = self._score_against(best, contrib_wo) - victim.static_score
            if net > 0:
                self._commit_remove(victim)
                self._commit_add(best, "swap")
            else:
                if await self._widen():
                    continue
                self.exit_reason = "best candidate cannot improve the deck"
                break
        else:
            self.exit_reason = "hit iteration cap"

        self._log_summary()
        return self.deck

    # ---- Trace / logs ----
    def _log_row(self, action: str, c: Candidate) -> None:
        # Logged right after the add/swap, so gap/contrib reflect the post-move state.
        log.info(
            "deck[%d] %-4s tier%d '%s' | cov=%.2f even=%.2f band=%.2f static=%.2f owners=%s "
            "| size=%d gap=%.2f contrib=%s",
            self.iterations,
            action,
            c.tier,
            (c.track.name or c.key)[:32],
            c.coverage,
            c.evenness,
            c.band,
            c.static_score,
            {p: round(w, 2) for p, w in c.owners.items()},
            len(self.deck),
            self._gap(self.contrib),
            {p: round(w, 2) for p, w in self.contrib.items()},
        )

    def _log_summary(self) -> None:
        tiers = {1: 0, 2: 0, 3: 0}
        for c in self.deck:
            tiers[c.tier] += 1
        log.info(
            "deck built: %d songs (tier1=%d tier2=%d tier3=%d) in %d iters; exit=%s; "
            "final gap=%.3f (tol=%.2f); contrib=%s; synthetic=%s",
            len(self.deck),
            tiers[1],
            tiers[2],
            tiers[3],
            self.iterations,
            self.exit_reason,
            self._gap(self.contrib),
            TOLERANCE,
            {p: round(w, 3) for p, w in self.contrib.items()},
            {p: round(w, 3) for p, w in self.synthetic_contrib.items()},
        )


@dataclass
class ExclusionGroup:
    """One past session's played songs, as an excludable unit.

    Grouping matters for relaxation: when a deck comes out short we drop whole sessions
    oldest-first, which is both intuitive ("we let the older night back in") and stable,
    rather than dropping individual songs by some arbitrary order."""

    session_id: str
    started_at: float
    spotify_ids: set[str] = field(default_factory=set)
    isrcs: set[str] = field(default_factory=set)


async def build_deck(
    libraries: list[PlayerLibrary],
    clusters_by_player: dict[str, PlayerClusters],
    resolve_token: str | None,
    exclude_spotify_ids: set[str] | None = None,
    seed_spotify_ids: set[str] | None = None,
    exclusion_groups: list[ExclusionGroup] | None = None,
) -> tuple[list[Candidate], dict]:
    """Build the game deck. Returns `(deck, diagnostics)`.

    `exclude_spotify_ids` / `seed_spotify_ids` drive in-game "Play again": exclude the songs
    already played this sitting, seed the prior deck's unplayed songs, then top up.

    `exclusion_groups` are past sessions the players chose to skip. If honouring all of them
    leaves the deck below `RELAX_FILL_FRACTION` of its soft size, the build is retried with the
    oldest group dropped, repeatedly, until the deck fills or no groups remain — so a group with
    a long history never ends up with a stunted deck. Which sessions actually got honoured is
    reported in the diagnostics."""
    groups = sorted(exclusion_groups or [], key=lambda g: g.started_at)  # oldest first
    attempts: list[dict] = []

    while True:
        excluded_isrcs = {i for g in groups for i in g.isrcs}
        excluded_ids = set(exclude_spotify_ids or set())
        excluded_ids |= {i for g in groups for i in g.spotify_ids}

        builder = _DeckBuilder(
            libraries,
            clusters_by_player,
            resolve_token,
            exclude_spotify_ids=excluded_ids,
            seed_spotify_ids=seed_spotify_ids,
            exclude_isrcs=excluded_isrcs,
        )
        deck = await builder.build()
        attempts.append(
            {"sessions_excluded": len(groups), "deck_size": len(deck)}
        )

        if len(deck) >= RELAX_FILL_FRACTION * builder.soft or not groups:
            break
        dropped = groups.pop(0)  # oldest session's exclusions come back
        log.info(
            "deck: only %d/%d songs with %d sessions excluded — relaxing, dropping session %s",
            len(deck), builder.soft, len(groups) + 1, dropped.session_id,
        )

    tiers = {1: 0, 2: 0, 3: 0}
    for c in deck:
        tiers[c.tier] += 1
    diagnostics = {
        "players": builder.players,
        "soft_size": builder.soft,
        "deck_size": len(deck),
        "tier_counts": tiers,
        "exit_reason": builder.exit_reason,
        "iterations": builder.iterations,
        "final_gap": round(builder._gap(builder.contrib), 4),
        "tolerance": TOLERANCE,
        "player_contributions": {p: round(w, 4) for p, w in builder.contrib.items()},
        "synthetic_contributions": {
            p: round(w, 4) for p, w in builder.synthetic_contrib.items()
        },
        # Play-history exclusion: which sessions were honoured, and what relaxing took.
        "excluded_sessions": [g.session_id for g in groups],
        "excluded_songs": len({i for g in groups for i in g.spotify_ids}),
        "relaxed": len(attempts) > 1,
        "build_attempts": attempts,
    }
    return deck, diagnostics
