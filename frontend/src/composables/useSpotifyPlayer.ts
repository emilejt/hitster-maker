// In-game track playback via the Spotify Web Playback SDK.
//
// The SDK spins up a virtual Spotify device *inside the browser tab* for one account, then we
// start/pause tracks on it through the Web API. That account must have Spotify **Premium** and
// the token must carry the `streaming` + `user-modify-playback-state` scopes (see
// spotify_service SCOPES). We deliberately never autoplay — the GameView play/pause button is
// the only thing that starts audio.
//
// Kept framework-light: `connect(token)` is called once from GameView's onMounted (after the
// deck handoff is read), and the returned refs drive the button state.

import { onUnmounted, ref } from 'vue'

const SDK_SRC = 'https://sdk.scdn.co/spotify-player.js'

// The SDK is a global script with a single global ready-callback; load it at most once per tab.
let sdkReady: Promise<void> | null = null
function loadSdk(): Promise<void> {
  if (window.Spotify) return Promise.resolve()
  if (sdkReady) return sdkReady
  sdkReady = new Promise<void>((resolve) => {
    window.onSpotifyWebPlaybackSDKReady = () => resolve()
    const script = document.createElement('script')
    script.src = SDK_SRC
    script.async = true
    document.body.appendChild(script)
  })
  return sdkReady
}

export function useSpotifyPlayer() {
  const ready = ref(false) // SDK device registered and available to play on
  const isPlaying = ref(false)
  const error = ref<string | null>(null)

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let player: any = null
  let deviceId: string | null = null
  let token: string | null = null

  async function connect(accessToken: string | null): Promise<void> {
    token = accessToken
    if (!token) {
      error.value = 'No Spotify token available for playback.'
      return
    }
    try {
      await loadSdk()
      player = new window.Spotify.Player({
        name: 'Hitster Maker',
        getOAuthToken: (cb: (t: string) => void) => cb(token as string),
        volume: 0.8,
      })
      player.addListener('ready', ({ device_id }: { device_id: string }) => {
        deviceId = device_id
        ready.value = true
        error.value = null
      })
      player.addListener('not_ready', () => {
        ready.value = false
      })
      player.addListener(
        'player_state_changed',
        (state: { paused: boolean } | null) => {
          if (state) isPlaying.value = !state.paused
        },
      )
      player.addListener('initialization_error', ({ message }: { message: string }) => {
        error.value = message
      })
      player.addListener('authentication_error', ({ message }: { message: string }) => {
        error.value = `Spotify auth error: ${message}`
      })
      player.addListener('account_error', () => {
        error.value = 'Spotify Premium is required to play tracks in the browser.'
      })
      await player.connect()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  /** Start a specific track from the beginning on our device. */
  async function playUri(uri: string): Promise<void> {
    if (!deviceId || !token) return
    try {
      const res = await fetch(
        `https://api.spotify.com/v1/me/player/play?device_id=${deviceId}`,
        {
          method: 'PUT',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ uris: [uri] }),
        },
      )
      if (!res.ok && res.status !== 204) {
        error.value = `Couldn't start playback (${res.status}).`
        return
      }
      isPlaying.value = true
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function pause(): Promise<void> {
    if (!player) return
    await player.pause()
    isPlaying.value = false
  }

  async function resume(): Promise<void> {
    if (!player) return
    await player.resume()
    isPlaying.value = true
  }

  onUnmounted(() => {
    player?.disconnect()
  })

  return { ready, isPlaying, error, connect, playUri, pause, resume }
}
