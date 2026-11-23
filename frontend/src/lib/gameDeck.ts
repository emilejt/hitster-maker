// Hand-off channel for the built game deck: Start Game builds the pool, stashes it here, then
// navigates to /game which reads it. sessionStorage (tab-scoped), mirroring lobbyState.ts.

import type { PoolSong } from './api'

export interface GameHandoff {
  pool: PoolSong[]
  players: { id: string; username: string }[]
  // One connected player's Spotify token, used to drive in-game Web Playback SDK audio.
  playbackToken?: string | null
  // The lobby the deck was built from — so "Play again" can rebuild against the same libraries.
  lobbyId: string
  // The game session plays are recorded against (see lib/api.ts createSession). Null if the
  // session couldn't be opened — the game still runs, it just isn't remembered.
  sessionId?: string | null
}

const KEY = 'hitster:deck'

export function saveGameDeck(handoff: GameHandoff): void {
  sessionStorage.setItem(KEY, JSON.stringify(handoff))
}

export function loadGameDeck(): GameHandoff | null {
  const raw = sessionStorage.getItem(KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as GameHandoff
  } catch {
    return null
  }
}

export function clearGameDeck(): void {
  sessionStorage.removeItem(KEY)
}
