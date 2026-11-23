<script setup lang="ts">
// Landing route for the Spotify OAuth callback. The backend redirects here with either
// `?access_token=…&player_id=…` (success) or `?error=…` (failure). On success we stash
// the connection and immediately replace the URL with the route that started the connect
// (the lobby /start, the taste tool /analysis, …) so the token never lingers in history;
// on failure we show a message with a way back.
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NSpin } from 'naive-ui'
import { consumeReturnPath, saveSpotifyConnection } from '../lib/spotifyConnection'

const route = useRoute()
const router = useRouter()

const error = ref<string | null>(null)
// Whichever view initiated the connect (default /start for a cold landing).
const returnPath = consumeReturnPath() ?? '/start'

onMounted(() => {
  const { access_token, player_id, error: err } = route.query

  if (typeof err === 'string') {
    error.value = err
    return
  }

  if (typeof access_token === 'string') {
    saveSpotifyConnection({
      playerId: typeof player_id === 'string' ? player_id : '',
      accessToken: access_token,
    })
  }

  // Back to the initiating view (which consumes the connection). `replace` strips the
  // token from the browser history.
  router.replace(returnPath)
})
</script>

<template>
  <section
    class="flex min-h-screen flex-col items-center justify-center gap-6 bg-black px-6 text-center"
  >
    <template v-if="error">
      <h1 class="text-2xl font-bold text-white">Spotify connection failed</h1>
      <p class="text-gray-400">
        Something went wrong (<span class="text-white">{{ error }}</span
        >). You can try connecting again from the lobby.
      </p>
      <n-button type="primary" round @click="router.replace(returnPath)">
        Go back
      </n-button>
    </template>

    <template v-else>
      <n-spin size="large" />
      <p class="text-gray-400">Connecting to Spotify…</p>
    </template>
  </section>
</template>
