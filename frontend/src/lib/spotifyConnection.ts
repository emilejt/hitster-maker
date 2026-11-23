// Handoff channel for the Spotify OAuth round-trip.
//
// OAuth is a full-page browser redirect (not an SPA navigation), so the lobby's
// in-memory state is wiped when the user comes back. The /spotify landing route stashes
// the connection result here; the lobby (/start) consumes it on return and attaches the
// token to the matching player. sessionStorage (not localStorage) so it's scoped to the
// tab and cleared when it closes.

const KEY = 'hitster:spotify-connection'
const RETURN_KEY = 'hitster:return-path'

export interface SpotifyConnection {
  /** The lobby player this token belongs to (echoed back from the OAuth `state`). */
  playerId: string
  /** Spotify access token — kept client-side for in-game playback. */
  accessToken: string
}

// Which app route initiated the connect. OAuth is a full-page redirect and the /spotify
// landing route needs to send the user back to wherever they started (the lobby /start, the
// taste tool /analysis, …) so that view's composable can consume the connection. Stored
// right before the redirect; read one-shot on return.

/** Called before the OAuth redirect with the current route path. */
export function saveReturnPath(path: string): void {
  sessionStorage.setItem(RETURN_KEY, path)
}

/** Called by the /spotify landing route; reads and clears the pending return path. */
export function consumeReturnPath(): string | null {
  const path = sessionStorage.getItem(RETURN_KEY)
  sessionStorage.removeItem(RETURN_KEY)
  return path
}

/** Called by the /spotify landing route once OAuth succeeds. */
export function saveSpotifyConnection(conn: SpotifyConnection): void {
  sessionStorage.setItem(KEY, JSON.stringify(conn))
}

/** Called by the lobby on return; reads and clears the pending connection (one-shot). */
export function consumeSpotifyConnection(): SpotifyConnection | null {
  const raw = sessionStorage.getItem(KEY)
  if (!raw) return null
  sessionStorage.removeItem(KEY)
  try {
    return JSON.parse(raw) as SpotifyConnection
  } catch {
    return null
  }
}
