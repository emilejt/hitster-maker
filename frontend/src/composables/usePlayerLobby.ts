// The lobby connect + extraction flow, shared by the game lobby (StartAGameView) and the
// taste-analysis tool. It owns: creating a lobby, adding players, real Spotify OAuth (a
// full-page redirect), rehydrating on return, and the Profile Maker (top tracks + playlists). State survives the redirect via
// sessionStorage (lobbyState.ts / spotifyConnection.ts).
//
// Consumers own only their own presentation: this composable is DOM-ref-free.

import { computed, onMounted, ref } from 'vue'

import {
  addPlayer as apiAddPlayer,
  addSources as apiAddSources,
  createLobby,
  getLobbyHistory,
  getPlaylists,
  loginUrl,
  type HistorySession,
  type PlayerSummary,
  type SpotifyPlaylist,
} from '../lib/api'
import {
  loadLobbyState,
  saveLobbyState,
  type StoredPlayer,
} from '../lib/lobbyState'
import { consumeSpotifyConnection, saveReturnPath } from '../lib/spotifyConnection'

/** Runtime player = persisted fields + transient UI state (not stored). */
export interface Player extends StoredPlayer {
  busy?: boolean
  error?: string | null
  summary?: PlayerSummary
}

export const MIN_PLAYERS = 2
export const MAX_PLAYERS = 5

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export function usePlayerLobby() {
  const lobbyId = ref('')
  const lobbyError = ref<string | null>(null)
  const players = ref<Player[]>([])
  let nextId = 0

  const canAddMore = computed(() => players.value.length < MAX_PLAYERS)
  const connectedCount = computed(
    () => players.value.filter((p) => p.status === 'connected').length,
  )
  const canStart = computed(() => connectedCount.value >= MIN_PLAYERS)

  // --- Persistence (survives the full-page OAuth redirect) -------------------

  function persistState() {
    saveLobbyState({
      lobbyId: lobbyId.value,
      nextId,
      players: players.value.map(
        ({ id, username, status, accessToken, sources }): StoredPlayer => ({
          id,
          username,
          status,
          accessToken,
          sources,
        }),
      ),
    })
  }

  function restoreState() {
    const s = loadLobbyState()
    if (!s) return
    lobbyId.value = s.lobbyId
    nextId = s.nextId
    players.value = s.players.map((p) => ({ ...p, busy: false, error: null }))
  }

  async function ensureLobby() {
    if (lobbyId.value) return
    const { lobby_id } = await createLobby()
    lobbyId.value = lobby_id
    persistState()
  }

  // --- Adding players --------------------------------------------------------

  /** Add a player from a (raw) username. Returns the created player, or null if the name
   * was blank or the lobby is full — callers use that to decide whether to clear the input. */
  function addPlayer(name: string): Player | null {
    const trimmed = name.trim()
    if (!trimmed || !canAddMore.value) return null
    const player: Player = { id: `p${nextId++}`, username: trimmed, status: 'new' }
    players.value.push(player)
    persistState()
    return player
  }

  // --- Spotify connect (real OAuth) ------------------------------------------

  async function connectSpotify(player: Player) {
    // Already connected once this session — just reopen the profile modal, no re-auth.
    if (player.status === 'ready') {
      void openSourceModal(player)
      return
    }
    try {
      await ensureLobby()
    } catch (e) {
      lobbyError.value = `Couldn't create a lobby (${errMsg(e)}).`
      return
    }
    persistState() // must survive the redirect
    saveReturnPath(window.location.pathname) // come back to whichever view started the connect
    window.location.href = loginUrl(player.id)
  }

  /** Back from Spotify: register the player + fetch top tracks, then open Profile Maker. */
  async function handleReturnFromSpotify(playerId: string, accessToken: string) {
    const player = players.value.find((p) => p.id === playerId)
    if (!player) return
    player.accessToken = accessToken
    player.error = null
    player.busy = true
    try {
      player.summary = await apiAddPlayer(lobbyId.value, {
        player_id: player.id,
        username: player.username,
        access_token: accessToken,
      })
      player.status = 'ready'
      persistState()
      await openSourceModal(player)
    } catch (e) {
      player.error = errMsg(e)
    } finally {
      player.busy = false
    }
  }

  // --- Profile Maker modal ---------------------------------------------------

  const sourceModalPlayer = ref<Player | null>(null)
  const showPlaylists = ref(false)
  // Liked songs are hidden from the Profile Maker for now — kept wired but pinned to 0 so no
  // liked tracks are fetched. (Restore the UI to re-enable; the backend plumbing is intact.)
  const likedCount = ref(0)
  // Top songs are fetched on connect but only *included* if this stays ticked — untick it for a
  // playlists-only profile.
  const includeTopTracks = ref(true)
  const selectedPlaylists = ref<string[]>([])
  const playlists = ref<SpotifyPlaylist[]>([])
  const playlistsLoading = ref(false)
  const playlistsError = ref<string | null>(null)
  const buildingProfile = ref(false)
  const profileError = ref<string | null>(null)

  async function openSourceModal(player: Player) {
    sourceModalPlayer.value = player
    showPlaylists.value = false
    likedCount.value = player.sources?.likedCount ?? 0
    includeTopTracks.value = player.sources?.includeTopTracks ?? true
    selectedPlaylists.value = player.sources ? [...player.sources.playlistIds] : []
    playlists.value = []
    playlistsError.value = null
    profileError.value = null
    if (!player.accessToken) return
    playlistsLoading.value = true
    try {
      playlists.value = await getPlaylists(player.accessToken)
    } catch (e) {
      playlistsError.value = errMsg(e)
    } finally {
      playlistsLoading.value = false
    }
  }

  function closeSourceModal() {
    sourceModalPlayer.value = null
  }

  /** A profile needs at least one source — the backend rejects an empty one, so don't offer it. */
  const canBuildProfile = computed(
    () => includeTopTracks.value || selectedPlaylists.value.length > 0,
  )

  async function buildProfile() {
    const player = sourceModalPlayer.value
    if (!player || !lobbyId.value || !canBuildProfile.value) return
    buildingProfile.value = true
    profileError.value = null
    try {
      player.summary = await apiAddSources(lobbyId.value, player.id, {
        liked_count: likedCount.value,
        playlist_ids: [...selectedPlaylists.value],
        include_top_tracks: includeTopTracks.value,
      })
      player.sources = {
        likedCount: likedCount.value,
        playlistIds: [...selectedPlaylists.value],
        includeTopTracks: includeTopTracks.value,
      }
      player.status = 'connected'
      persistState()
      closeSourceModal()
      void refreshHistory() // this player's past games may now be skippable
    } catch (e) {
      profileError.value = errMsg(e)
    } finally {
      buildingProfile.value = false
    }
  }

  // --- Play history (past games these players can skip) ----------------------
  //
  // Keyed on the players themselves, not on the lobby: a "lobby" is a random id that dies in an
  // hour, whereas "Alice heard this song" stays true. The backend returns every session any
  // connected player took part in, so a group of 3 + a newcomer still skips what the 3 heard.

  const historySessions = ref<HistorySession[]>([])
  const excludedSessionIds = ref<string[]>([])
  const historyLoading = ref(false)
  const historyError = ref<string | null>(null)

  /** Reload the history list, pre-ticking sessions inside the backend's recent window.
   * Ticks the user has already changed are preserved across reloads. */
  async function refreshHistory() {
    if (!lobbyId.value || connectedCount.value === 0) return
    historyLoading.value = true
    historyError.value = null
    try {
      const { sessions } = await getLobbyHistory(lobbyId.value)
      const known = new Set(historySessions.value.map((s) => s.session_id))
      historySessions.value = sessions
      // Newly-seen recent sessions start ticked; anything the host already decided stays.
      const fresh = sessions
        .filter((s) => s.recent && !known.has(s.session_id))
        .map((s) => s.session_id)
      const valid = new Set(sessions.map((s) => s.session_id))
      excludedSessionIds.value = [
        ...excludedSessionIds.value.filter((id) => valid.has(id)),
        ...fresh,
      ]
    } catch (e) {
      historyError.value = errMsg(e)
    } finally {
      historyLoading.value = false
    }
  }

  // --- Lifecycle: restore, ensure a lobby, pick up a returning connection -----

  onMounted(async () => {
    restoreState()
    try {
      await ensureLobby()
    } catch (e) {
      lobbyError.value = `Couldn't reach the backend to create a lobby (${errMsg(e)}).`
      return
    }
    const conn = consumeSpotifyConnection()
    if (conn) await handleReturnFromSpotify(conn.playerId, conn.accessToken)
    // Coming back from OAuth with players already connected — the list is worth having.
    if (connectedCount.value > 0) void refreshHistory()
  })

  return {
    // policy
    MIN_PLAYERS,
    MAX_PLAYERS,
    // state
    lobbyId,
    lobbyError,
    players,
    canAddMore,
    connectedCount,
    canStart,
    // actions
    addPlayer,
    connectSpotify,
    // Profile Maker
    sourceModalPlayer,
    showPlaylists,
    likedCount,
    includeTopTracks,
    selectedPlaylists,
    playlists,
    playlistsLoading,
    playlistsError,
    buildingProfile,
    profileError,
    canBuildProfile,
    openSourceModal,
    closeSourceModal,
    buildProfile,
    // Play history
    historySessions,
    excludedSessionIds,
    historyLoading,
    historyError,
    refreshHistory,
  }
}
