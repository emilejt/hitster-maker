# frontend

Vue 3 + Vite + TypeScript, with Naive UI and Tailwind v4. Normally started by the root
`docker-compose.yml` (port 5173); see the main [README](../README.md).

Without Docker:

```bash
npm install
npm run dev     # http://localhost:5173
npm run build   # type-check (vue-tsc) + production build
```

The API base URL comes from `VITE_API_BASE_URL` and defaults to `http://127.0.0.1:8000`.

| Path | What it is |
|---|---|
| `views/StartAGameView.vue` | the lobby: add players, connect Spotify, build profiles, skip past games |
| `views/GameView.vue` | the game: turn rotation, place → reveal → score, Spotify playback |
| `views/TasteAnalysisView.vue` | `/analysis`, the dev tool used to tune the embedding tiers |
| `composables/usePlayerLobby.ts` | connect + extraction + profile flow shared by the lobby and `/analysis` |
| `composables/useSpotifyPlayer.ts` | Spotify Web Playback SDK wrapper (needs Premium; never autoplays) |
| `lib/` | API clients and the `sessionStorage` handoffs that survive the OAuth redirect |
