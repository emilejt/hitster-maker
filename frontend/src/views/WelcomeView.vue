<script setup lang="ts">
import { NButton } from 'naive-ui'
import { useRouter } from 'vue-router'
import { clearLobbyState } from '../lib/lobbyState'

const router = useRouter()

function startGame() {
  // Fresh game: drop any lobby left in this tab from a previous session.
  clearLobbyState()
  router.push('/start')
}
</script>

<template>
  <section
    class="relative flex min-h-screen flex-col items-center justify-center gap-10 overflow-hidden bg-black px-6 text-center"
  >
    <!-- Soft glowing green blobs in the background for a bit of depth -->
    <div
      class="pointer-events-none absolute -top-32 -left-32 h-96 w-96 rounded-full bg-green-500/20 blur-3xl"
    />
    <div
      class="pointer-events-none absolute -bottom-32 -right-32 h-96 w-96 rounded-full bg-emerald-600/15 blur-3xl"
    />

    <div class="relative flex flex-col items-center gap-4">
      <span class="text-6xl drop-shadow-lg">🎵</span>

      <h1
        class="bg-gradient-to-r from-green-400 via-emerald-400 to-green-500 bg-clip-text text-6xl font-extrabold tracking-tight text-transparent sm:text-7xl"
      >
        Hitster Maker
      </h1>

      <p class="max-w-md text-lg text-gray-400">
        Build a music-guessing deck from what your group actually listens to.
        Gather your friends, connect Spotify, and see who really knows their tunes.
      </p>
    </div>

    <n-button
      type="primary"
      size="large"
      round
      class="relative px-10 py-6 text-lg font-semibold"
      @click="startGame"
    >
      Start a Game
    </n-button>

    <p class="relative text-xs uppercase tracking-widest text-gray-600">
      2–5 players · Spotify required
    </p>
  </section>
</template>
