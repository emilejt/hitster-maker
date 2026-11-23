// Minimal ambient types for the Spotify Web Playback SDK global, which is loaded at runtime
// from a <script> tag (see useSpotifyPlayer.ts) rather than an npm package. We only type the
// surface we use; the player object itself is intentionally loose.
export {}

declare global {
  interface Window {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    Spotify: any
    onSpotifyWebPlaybackSDKReady: () => void
  }
}
