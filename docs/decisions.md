# Design decisions

The choices that shaped the project, with the reasoning. Each one was a real fork in the road.

### Train a song embedding instead of relying on Spotify
Spotify no longer gives new apps recommendations, audio features, related artists or track
popularity. Any notion of "these two people's tastes overlap" had to be learned from open
data. ListenBrainz publishes raw listening histories, so item2vec on that data was the most
direct route. See [song2vec.md](song2vec.md).

### One month of recent ListenBrainz data
The model is trained on the 30 daily incremental dumps from 10 May to 9 June 2026, not on
the full multi-year history. That kept preprocessing and training within Kaggle sessions
(the training run alone took about 1 h 50 min). Recent listens also carry a MusicBrainz ID
far more often than old ones. The cost is a small corpus with a recency bias; see the
limitations in [song2vec.md](song2vec.md).

### A "sentence" is one user's listening in one calendar week
Weekly windows capture *taste zones* ("these songs live in the same world"), which is what
recognition depends on. Session windows (30-minute gaps) capture playlist adjacency, which is
a narrower signal. Weekly windows are also simpler and match what MetaBrainz uses on this
data. Comparing the two remains the most interesting open experiment.

### Song-level embeddings directly, no artist-level warm-up
The original plan trained artist embeddings first "to validate the pipeline". That would have
meant writing the same pipeline twice, so it was dropped in favour of training at song level
straight away.

### Skip-gram, not CBOW
The catalogue is long-tailed: half the vocabulary appears fewer than 50 times. CBOW averages
the context and washes out rare items; skip-gram gives every occurrence its own training
signal.

### Match on ISRC; use the embedding only as a fallback
A song in two players' libraries is *evidence* that both know it; embedding proximity is only
an *estimate*. So matching is literal ISRC intersection with no embedding involved, and the
embedding only enters when real matches run out (tiers 2–3), never ahead of them.

### Several taste zones per player, from a global distance cut
A single average vector puts a metal-and-jazz listener between the two, close to neither.
K-means needed a hand-picked k per player. Agglomerative clustering cut at one global cosine
distance (0.7) makes the number of zones an output, so an eclectic player gets more zones, and
a zone is equally tight for everyone. Clusters under 30 songs are treated as noise.

### Greedy, fairness-driven deck building
The game only works if every player knows a similar share of the cards. A transparent greedy
loop with logged, hand-tunable terms (coverage, evenness, difficulty band, fairness gap) was
easier to reason about and tune than an opaque optimiser. See [deck-builder.md](deck-builder.md).

### Tier 3 (synthetic songs) built but disabled
A synthetic card is in nobody's library, so its worst case is a card nobody recognizes, the
worst possible outcome in this game. It stays off until its recognition rate has been
measured.

### Spotify only; Last.fm dropped
Last.fm was the original primary source (no 5-user developer cap), but it only records plays
from the moment you connect it and has no backfill, so it is useless for new users. The
Last.fm integration was removed.

### A custom popularity score
Spotify's track `popularity` field is deprecated. Popularity is the number of unique
ListenBrainz listeners per recording, log-normalized to [0, 1], computed from the same data
as the embedding.
