// Persisted lobby state. OAuth is a full-page redirect that wipes in-memory component
// state, so the lobby (players, tokens, the lobby id) is mirrored to sessionStorage and
// rehydrated when the user lands back on /start. Scoped to the tab; cleared when a new
// game is started from the welcome screen.

export type PlayerStatus = 'new' | 'ready' | 'connected'
// 'new'       — added, not yet connected to Spotify
// 'ready'     — connected + top tracks fetched, taste profile not built yet
// 'connected' — profile built (liked + playlists in)

export interface StoredPlayer {
  id: string
  username: string
  status: PlayerStatus
  accessToken?: string
  sources?: { likedCount: number; playlistIds: string[]; includeTopTracks: boolean }
}

export interface LobbyState {
  lobbyId: string
  nextId: number
  players: StoredPlayer[]
}

const KEY = 'hitster:lobby'

export function saveLobbyState(state: LobbyState): void {
  sessionStorage.setItem(KEY, JSON.stringify(state))
}

export function loadLobbyState(): LobbyState | null {
  const raw = sessionStorage.getItem(KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as LobbyState
  } catch {
    return null
  }
}

export function clearLobbyState(): void {
  sessionStorage.removeItem(KEY)
}
