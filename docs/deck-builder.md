# The deck builder

*Code: [`pipeline/deck.py`](../backend/app/pipeline/deck.py), with
[`matching.py`](../backend/app/pipeline/matching.py),
[`scoring.py`](../backend/app/pipeline/scoring.py) and
[`clustering.py`](../backend/app/pipeline/clustering.py).*

A deck is a set of cards (song → guess its release year) chosen so that **every player
contributes about equally**. A player "contributes" the songs in the deck they are likely to
know, weighted by how well they are likely to know them. If one player knows every card, the
game isn't fun. If nobody knows a card, it isn't fun either.

The builder is **a single greedy loop over a candidate pool that widens when it runs dry**.
It is not three phases run one after another.

## 1. The unit: occurrence weight

Every decision is expressed in one number: how strongly does this player own this song
([`scoring.occurrence_weight`](../backend/app/pipeline/scoring.py))?

| Source | Weight |
|---|---|
| Top tracks | $1 / (1 + (\text{rank}/r_0)^{1.3})$: rank 0 → 1.0, rank $r_0$ → 0.5, then decays |
| $r_0$ by time range | short-term 60 · medium-term 150 · long-term 350 |
| Playlist track | flat 0.3 |

$r_0$ grows with the time range because long-term libraries run much deeper: being someone's
350th all-time track still means something, while their 60th track this month already says
little. When a song was *saved* (`added_at`) is deliberately ignored, since it says little
about how well the song is known. If a player has the same song from several sources, only
their strongest copy counts.

## 2. Score of a candidate song

$$
\text{score} = 1.0\cdot\text{coverage} + 1.0\cdot\text{evenness} + 0.7\cdot\text{band} + 3.0\cdot\text{gapClosing}
$$

| Term | Definition | Favours |
|---|---|---|
| coverage | $(\text{owners}-1)/(\text{players}-1)$ | songs more people know |
| evenness | $1 - (\max w - \min w)$ over the song's owners | songs its owners know equally well |
| band | $\exp(-((\bar w - 0.6)/0.2)^2)$ | mid-strength songs: not everyone's #1, not a vague memory |
| gapClosing | $\text{gap}_\text{before} - \text{gap}_\text{after}$, where gap = max − min of the running per-player totals | songs that help whoever is behind |

The first three terms depend only on the song and are computed once. Only `gapClosing` changes
between iterations, which keeps the loop cheap.

## 3. The loop

```
loop:
  best = highest-scoring unused candidate from the unlocked tiers
  no candidates, or score(best) < FLOOR (0.5)  → unlock the next tier, or stop
  deck smaller than soft size (15 × players)   → add best
  deck fair (gap ≤ TOLERANCE = 1.0)            → stop
  otherwise                                    → swap the leading player's least valuable card
                                                 for best, if that raises the total score
```

Two consequences:

- **The deck size is soft.** The deck stops early once it is fair, and when it is unfair it
  swaps cards rather than growing, so it converges on fairness rather than on a fixed length.
- **Tiers only widen.** The builder never *prefers* a fallback song: tier-1 songs stay in the
  pool and compete on equal terms. Fallbacks only become available when real shared songs can
  no longer clear the floor.

Every add and swap logs a trace row (the score terms, the running totals), and each build logs
a summary. These traces are how the constants were tuned.

## 4. Where candidates come from

Each tier is built in one batch when it unlocks. Nothing is generated per iteration.

**Tier 1: real matches.** Songs (by ISRC) present in at least two players' libraries, each
owner carrying their measured weight. No embedding involved: matching is literal.

**Tier 2: bridge songs.** Songs only one player owns, scored by how close they sit to another
player's taste in the embedding ([song2vec.md](song2vec.md) §4). The owner gets their real
weight; the closest other player gets an estimate of $0.5 \times$ cosine similarity to their
nearest taste centroid. Only the player currently behind has their bridge songs offered. This
uses NumPy over vectors already in memory and one batched SQLite lookup, with no Spotify calls.

**Tier 3: synthetic songs (built, switched off).** Popular songs that *nobody* owns, near a
blend of two players' overlapping taste centroids. A popularity gate is applied before any
Spotify lookup, and no player's total may be more than 30% synthetic. `TIER3_ENABLED = False`.
Tiers 1 and 2 always have at least one real owner, so their worst case is a card one player
knows and another doesn't, which is a normal game. A tier-3 card can be one nobody at the table
recognizes. That needs measuring before tier 3 can be trusted (song2vec.md §6), along with a
stricter unlock rule, e.g. only to rescue a deck that is far below size.

## 5. Not replaying songs

- **Play again (same evening):** songs already played are excluded from every tier. The
  previous deck's unplayed songs are put back first, with their real weights, and the loop
  tops the deck up.
- **Past game nights:** the lobby lists past sessions involving any of the current players.
  Recent ones (last 30 days) are pre-ticked, and ticked sessions are excluded. Matching is
  **by ISRC first, Spotify id second**: one recording has several Spotify ids across markets
  and re-releases.
- **Relaxation:** a group burns 15 shared songs per player per game, so a long history can
  starve the deck. If a build comes out below 80% of the soft size, the builder drops the
  **oldest** excluded session and rebuilds, repeating until the deck fills. Tiers 1–2 are pure
  in-memory work, so a rebuild costs almost nothing. The diagnostics report which sessions
  were actually honoured.

## 6. Tunables

All of them are plain module constants at the top of each file.

| Constant | Value | File |
|---|---|---|
| top-track $r_0$ / steepness | 60 · 150 · 350 / 1.3 | `scoring.py` |
| playlist weight | 0.3 | `scoring.py` |
| `CLUSTER_CUT` / `CLUSTER_MIN_SIZE` | 0.7 / 30 | `clustering.py` |
| score coefficients A / B / C / D | 1.0 / 1.0 / 0.7 / 3.0 | `deck.py` |
| `BAND_CENTER` / `BAND_WIDTH` | 0.6 / 0.2 | `deck.py` |
| `SOFT_SIZE_PER_PLAYER` | 15 | `deck.py` |
| `FLOOR` / `TOLERANCE` | 0.5 / 1.0 (first-pass values) | `deck.py` |
| `BRIDGE_DISCOUNT` | 0.5 | `deck.py` |
| `RELAX_FILL_FRACTION` | 0.8 | `deck.py` |
| `SYNTHETIC_CAP`, `_TIER3_*` | 0.30, see file | `deck.py` |

The clustering constants were chosen with the [taste-analysis tool](architecture.md#the-taste-analysis-tool-analysis).
The tool shows how a real library splits into taste zones, what each zone's centroid pulls
from the embedding, and how close two players' zones are.
