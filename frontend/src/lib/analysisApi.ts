// Client for the dev-facing taste-analysis endpoints (backend `/analysis`). One-player views:
// clusters + neighbors; two-player views: proximity + combine + nearest-seeds.

import { req } from './api'

/** Clustering method. `kmeans` uses `k`; `agglomerative` uses a single global cosine-distance
 * cut (`distance`), from which the number of clusters falls out. Method + distance are chosen
 * globally for the whole tool; `k` stays a per-player (or per-role) knob for k-means. */
export type ClusterMethod = 'kmeans' | 'agglomerative'

export interface ClusterMode {
  method: ClusterMethod
  distance: number
  /** Agglomerative only: drop clusters with fewer than this many songs (k-means ignores it). */
  minClusterSize: number
}

/** The `method`/`distance`/`min_cluster_size` query fragment shared by the GET analysis calls. */
function modeQuery(mode: ClusterMode): string {
  return `method=${mode.method}&distance=${mode.distance}&min_cluster_size=${mode.minClusterSize}`
}

export interface ClusterSong {
  mbid: string
  name: string | null
  artist: string | null
  year: number | null
  similarity: number
}

export interface Cluster {
  index: number
  size: number
  songs: ClusterSong[]
}

export interface ClustersResponse {
  k: number
  method: ClusterMethod
  distance: number | null
  min_cluster_size: number | null
  dismissed: number | null
  silhouette: number | null
  seeds_total: number
  seeds_in_model: number
  clusters: Cluster[]
}

export interface Neighbor {
  mbid: string
  popularity: number
  name: string | null
  artist: string | null
  year: number | null
}

export interface NeighborCluster {
  index: number
  neighbors: Neighbor[]
}

export interface NeighborsResponse {
  k: number
  n: number
  clusters: NeighborCluster[]
}

/** One player's seed songs clustered into taste zones (metadata is free — from the library). */
export function getClusters(
  lobbyId: string,
  playerId: string,
  k: number,
  mode: ClusterMode,
): Promise<ClustersResponse> {
  return req(`/analysis/${lobbyId}/${playerId}/clusters?k=${k}&${modeQuery(mode)}`)
}

/** The n popularity-ranked model neighbours each of the player's centroids generates.
 * Names are resolved server-side (cache-first, then Spotify) — large n on a cold cache is slow. */
export function getNeighbors(
  lobbyId: string,
  playerId: string,
  k: number,
  n: number,
  mode: ClusterMode,
): Promise<NeighborsResponse> {
  return req(`/analysis/${lobbyId}/${playerId}/neighbors?k=${k}&n=${n}&${modeQuery(mode)}`)
}

// --- 2-player (F3) ------------------------------------------------------------

export interface ProximityPair {
  a_cluster: number
  b_cluster: number
  similarity: number
}

export interface ProximityResponse {
  k_a: number
  k_b: number
  pairs: ProximityPair[]
}

/** Every A-cluster × B-cluster cosine similarity, sorted strongest-first (the Mode-1 overlap
 * signal). The effective k's tell you how many clusters each player actually has. */
export function getProximity(
  lobbyId: string,
  a: string,
  b: string,
  kA: number,
  kB: number,
  mode: ClusterMode,
): Promise<ProximityResponse> {
  return req(`/analysis/${lobbyId}/proximity?a=${a}&b=${b}&k_a=${kA}&k_b=${kB}&${modeQuery(mode)}`)
}

export interface CombineResponse {
  n: number
  neighbors: Neighbor[]
}

/** Blend one cluster from each player and generate the shared-taste neighbours (Mode 2). */
export function combineClusters(
  lobbyId: string,
  body: {
    a: string
    a_cluster: number
    k_a: number
    b: string
    b_cluster: number
    k_b: number
    n: number
    method: ClusterMethod
    distance: number
    min_cluster_size: number
  },
): Promise<CombineResponse> {
  return req(`/analysis/${lobbyId}/combine`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export interface NearestSeed {
  mbid: string
  name: string | null
  artist: string | null
  year: number | null
  similarity: number
}

export interface NearestSeedsResponse {
  n: number
  k_base: number
  k_candidate: number
  songs: NearestSeed[]
}

/** The candidate cluster's own library songs closest to the base cluster's centroid.
 * Directional: swapping base/candidate gives a different result. Metadata is free (library). */
export function getNearestSeeds(
  lobbyId: string,
  body: {
    base: string
    base_cluster: number
    k_base: number
    candidate: string
    candidate_cluster: number
    k_candidate: number
    n: number
    method: ClusterMethod
    distance: number
    min_cluster_size: number
  },
): Promise<NearestSeedsResponse> {
  return req(`/analysis/${lobbyId}/nearest-seeds`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
