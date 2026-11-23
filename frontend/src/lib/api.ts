// Thin client for the backend. Base URL defaults to the loopback host Spotify requires
// (127.0.0.1, not localhost) and can be overridden with VITE_API_BASE_URL.

const BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

export async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ''}`)
  }
  return res.json() as Promise<T>
}

/** Per-player extraction summary returned by the lobby endpoints. */
export interface PlayerSummary {
  player_id: string
  username: string
  track_count: number
  unique_isrcs: number
  by_source: Record<string, number>
}

export interface SpotifyPlaylist {
  id: string
  name: string
}

/** Full-page navigation target that starts OAuth for one player. */
export function loginUrl(playerId: string): string {
  return `${BASE}/auth/spotify/login?player_id=${encodeURIComponent(playerId)}`
}

export function createLobby(): Promise<{ lobby_id: string }> {
  return req('/lobby', { method: 'POST' })
}

/** Call 2a — register the player and fetch their top tracks. */
export function addPlayer(
  lobbyId: string,
  body: { player_id: string; username: string; access_token: string },
): Promise<PlayerSummary> {
  return req(`/lobby/${lobbyId}/player`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** Call 2b — fetch the chosen sources and assemble the player's final library.
 * `include_top_tracks: false` drops the top tracks fetched on connect, for a playlists-only
 * profile. Rejected with a 400 if that would leave the library empty. */
export function addSources(
  lobbyId: string,
  playerId: string,
  body: { liked_count: number; playlist_ids: string[]; include_top_tracks: boolean },
): Promise<PlayerSummary> {
  return req(`/lobby/${lobbyId}/player/${playerId}/sources`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** One card in the built game deck (a song → guess the release year). */
export interface PoolSong {
  isrc: string | null
  spotify_id: string
  spotify_uri: string
  name: string | null
  artist: string | null
  year: number | null
  album_art: string | null
  owners: string[]
  tier: number // 1 match · 2 bridge · 3 synthetic
  from_fallback: boolean
}

export interface PoolResponse {
  pool: PoolSong[]
  diagnostics: Record<string, unknown>
}

/** Start Game / Play again — build the group deck from the lobby's stored libraries + clusters.
 *
 * Two kinds of memory: `excludeSpotifyIds`/`seedSpotifyIds` are the in-game Play-again state
 * (songs played this sitting, and the prior deck's leftovers to carry over), while
 * `excludeSessionIds` are past games the players ticked in the lobby's history list. */
export function buildPool(
  lobbyId: string,
  opts?: {
    excludeSpotifyIds?: string[]
    seedSpotifyIds?: string[]
    excludeSessionIds?: string[]
  },
): Promise<PoolResponse> {
  return req(`/lobby/${lobbyId}/pool`, {
    method: 'POST',
    body: JSON.stringify({
      exclude_spotify_ids: opts?.excludeSpotifyIds ?? [],
      seed_spotify_ids: opts?.seedSpotifyIds ?? [],
      exclude_session_ids: opts?.excludeSessionIds ?? [],
    }),
  })
}

/** One past game these players took part in, offered in the lobby's exclusion checklist. */
export interface HistorySession {
  session_id: string
  started_at: string // ISO
  players: string[]
  song_count: number
  /** Inside the backend's default window — pre-ticked for exclusion. */
  recent: boolean
}

/** Past sessions any connected player took part in, newest first. */
export function getLobbyHistory(
  lobbyId: string,
): Promise<{ window_days: number; sessions: HistorySession[] }> {
  return req(`/lobby/${lobbyId}/history`)
}

/** Open a game session — the backend reads the players' Spotify ids from the lobby itself. */
export function createSession(lobbyId: string): Promise<{ session_id: string }> {
  return req('/session', {
    method: 'POST',
    body: JSON.stringify({ lobby_id: lobbyId }),
  })
}

/** Record one song as played. Fire-and-forget: idempotent server-side, and a lost play should
 * never interrupt the game. */
export function recordPlay(
  sessionId: string,
  song: { spotify_id: string; isrc?: string | null; name?: string | null; artist?: string | null; year?: number | null },
): Promise<{ ok: boolean }> {
  return req(`/session/${sessionId}/played`, {
    method: 'POST',
    body: JSON.stringify(song),
  })
}

/** Mark a session finished (informational — its plays count either way). */
export function endSession(sessionId: string): Promise<{ ok: boolean }> {
  return req(`/session/${sessionId}/end`, { method: 'POST' })
}

/** The connected user's playlists, for the Profile Maker modal (name + id only). */
export async function getPlaylists(accessToken: string): Promise<SpotifyPlaylist[]> {
  const data = await req<{ items: Array<{ id: string; name: string } | null> }>(
    '/spotify/playlists?limit=50',
    { headers: { Authorization: `Bearer ${accessToken}` } },
  )
  return (data.items ?? [])
    .filter((p): p is { id: string; name: string } => !!p)
    .map((p) => ({ id: p.id, name: p.name }))
}
