<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  NButton,
  NCheckbox,
  NCheckboxGroup,
  NInput,
  NModal,
  NSpin,
} from 'naive-ui'
import type { InputInst } from 'naive-ui'
import { useRouter } from 'vue-router'

import { usePlayerLobby } from '../composables/usePlayerLobby'
import { buildPool, createSession } from '../lib/api'
import { saveGameDeck } from '../lib/gameDeck'

const router = useRouter()

// The whole connect + extraction flow lives in the composable; this view owns only its own
// input (username field + focus) and navigation.
const {
  MIN_PLAYERS,
  MAX_PLAYERS,
  lobbyId,
  lobbyError,
  players,
  canAddMore,
  connectedCount,
  canStart,
  addPlayer: addLobbyPlayer,
  connectSpotify,
  sourceModalPlayer,
  showPlaylists,
  selectedPlaylists,
  playlists,
  playlistsLoading,
  playlistsError,
  buildingProfile,
  profileError,
  closeSourceModal,
  buildProfile,
  includeTopTracks,
  canBuildProfile,
  historySessions,
  excludedSessionIds,
  historyLoading,
  historyError,
} = usePlayerLobby()

const username = ref('')
const usernameInput = ref<InputInst | null>(null)
const starting = ref(false)
const startError = ref<string | null>(null)

/** How many songs the currently ticked sessions would skip. Shown so the host can see the
 * cost of their choices before starting — the deck builder will relax these anyway if they'd
 * leave the deck short. */
const excludedSongCount = computed(() =>
  historySessions.value
    .filter((s) => excludedSessionIds.value.includes(s.session_id))
    .reduce((n, s) => n + s.song_count, 0),
)

function formatSessionDate(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
  })
}

function addPlayer() {
  const created = addLobbyPlayer(username.value)
  if (!created) return
  username.value = ''
  usernameInput.value?.focus()
}

async function startGame() {
  starting.value = true
  startError.value = null
  try {
    const { pool } = await buildPool(lobbyId.value, {
      excludeSessionIds: [...excludedSessionIds.value],
    })
    const connectedPlayers = players.value.filter((p) => p.status === 'connected')
    const connected = connectedPlayers.map((p) => ({ id: p.id, username: p.username }))
    // Audio plays from one account's browser device — use the first connected player's token.
    const playbackToken = connectedPlayers[0]?.accessToken ?? null
    // Open the session that plays get recorded against. A failure here must not block the
    // game — it just means this game won't be remembered for next time.
    let sessionId: string | null = null
    try {
      sessionId = (await createSession(lobbyId.value)).session_id
    } catch {
      sessionId = null
    }
    saveGameDeck({
      pool,
      players: connected,
      playbackToken,
      lobbyId: lobbyId.value,
      sessionId,
    })
    router.push('/game')
  } catch (e) {
    startError.value = e instanceof Error ? e.message : String(e)
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <section class="flex min-h-screen flex-col bg-black px-6 py-10 text-center">
    <!-- Middle: add-player controls -->
    <div class="flex flex-1 flex-col items-center justify-center gap-6">
      <div class="flex flex-col gap-1">
        <h1 class="text-4xl font-extrabold tracking-tight text-white">Who's playing?</h1>
        <p class="text-gray-400">
          Add {{ MIN_PLAYERS }}–{{ MAX_PLAYERS }} players, then connect each to Spotify.
        </p>
      </div>

      <p
        v-if="lobbyError"
        class="max-w-md rounded-lg border border-red-900 bg-red-950/50 px-4 py-2 text-sm text-red-300"
      >
        {{ lobbyError }}
      </p>

      <div class="flex w-full max-w-xs flex-col items-stretch gap-3">
        <n-input
          ref="usernameInput"
          v-model:value="username"
          placeholder="Enter a username"
          size="large"
          :maxlength="24"
          :disabled="!canAddMore"
          @keyup.enter="addPlayer"
        />
        <n-button
          type="primary"
          size="large"
          :disabled="!username.trim() || !canAddMore"
          @click="addPlayer"
        >
          Add Player
        </n-button>
        <p v-if="!canAddMore" class="text-xs text-gray-500">
          Maximum of {{ MAX_PLAYERS }} players reached.
        </p>
      </div>
    </div>

    <!-- Bottom: players section -->
    <div class="flex flex-col items-center gap-6">
      <ul v-auto-animate class="flex flex-wrap items-stretch justify-center gap-4">
        <!-- One card per added player -->
        <li
          v-for="player in players"
          :key="player.id"
          class="flex w-52 flex-col items-center gap-3 rounded-xl border border-neutral-800 bg-neutral-900 p-5"
        >
          <span class="max-w-full truncate text-lg font-semibold text-white">
            {{ player.username }}
          </span>

          <!-- Busy: registering + fetching top tracks -->
          <span v-if="player.busy" class="flex items-center gap-2 text-sm text-gray-400">
            <n-spin :size="16" />
            Fetching top songs…
          </span>

          <!-- Connected: profile built -->
          <template v-else-if="player.status === 'connected'">
            <span class="flex items-center gap-2 font-medium text-green-400">
              <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
              </svg>
              Connected
            </span>
            <span v-if="player.summary" class="text-xs text-gray-500">
              {{ player.summary.track_count }} songs · {{ player.summary.unique_isrcs }} unique
            </span>
          </template>

          <!-- Ready: has token + top tracks, still needs to build the profile -->
          <template v-else-if="player.status === 'ready'">
            <n-button type="primary" ghost round @click="connectSpotify(player)">
              Build profile
            </n-button>
            <span class="text-xs text-green-500/80">Spotify linked</span>
          </template>

          <!-- New: needs to connect -->
          <n-button v-else type="primary" round @click="connectSpotify(player)">
            <template #icon>
              <svg viewBox="0 0 24 24" class="h-5 w-5" fill="currentColor">
                <path
                  d="M12 2a10 10 0 100 20 10 10 0 000-20zm4.586 14.424a.622.622 0 01-.857.207c-2.348-1.435-5.304-1.76-8.785-.964a.623.623 0 11-.277-1.215c3.809-.87 7.077-.496 9.712 1.115a.623.623 0 01.207.857zm1.223-2.722a.78.78 0 01-1.072.257c-2.687-1.652-6.785-2.13-9.965-1.166a.78.78 0 11-.452-1.492c3.632-1.102 8.147-.568 11.232 1.329a.78.78 0 01.257 1.072zm.105-2.835C14.692 8.95 9.375 8.775 6.297 9.71a.935.935 0 11-.542-1.79c3.533-1.072 9.404-.865 13.115 1.338a.936.936 0 01-.956 1.61z"
                />
              </svg>
            </template>
            Connect to Spotify
          </n-button>

          <span v-if="player.error" class="text-xs text-red-400">{{ player.error }}</span>
        </li>

        <!-- Trailing empty slot — a dashed "next player" placeholder -->
        <li
          v-if="canAddMore"
          class="flex w-52 cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-neutral-700 p-5 text-neutral-600 transition-colors hover:border-green-600 hover:text-green-500"
          @click="usernameInput?.focus()"
        >
          <span class="text-3xl leading-none">+</span>
          <span class="text-sm">
            {{ players.length === 0 ? 'No players yet' : 'Next player' }}
          </span>
        </li>
      </ul>

      <!-- Past games these players can skip. Only shown when there's actual history. -->
      <div
        v-if="historySessions.length > 0"
        class="w-full max-w-lg rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 text-left"
      >
        <p class="font-medium text-white">You've played together before</p>
        <p class="mb-3 text-xs text-gray-500">
          Ticked games get skipped — {{ excludedSongCount }} songs won't come up again. Recent
          games are ticked for you. If that leaves too few songs, we'll add the oldest ones back.
        </p>

        <p v-if="historyError" class="text-sm text-red-400">
          Couldn't load past games ({{ historyError }}).
        </p>
        <div v-else-if="historyLoading" class="flex items-center gap-2 text-sm text-gray-400">
          <n-spin :size="16" /> Loading past games…
        </div>
        <n-checkbox-group v-else v-model:value="excludedSessionIds">
          <div class="flex max-h-44 flex-col gap-2 overflow-y-auto pr-1">
            <n-checkbox
              v-for="session in historySessions"
              :key="session.session_id"
              :value="session.session_id"
            >
              <span class="text-sm text-white">
                {{ formatSessionDate(session.started_at) }}
              </span>
              <span class="ml-2 text-xs text-gray-500">
                {{ session.players.join(', ') }} · {{ session.song_count }} songs
              </span>
            </n-checkbox>
          </div>
        </n-checkbox-group>
      </div>

      <!-- Start once at least MIN_PLAYERS have built a profile -->
      <n-button
        v-if="canStart"
        type="primary"
        size="large"
        round
        class="px-10 font-semibold"
        :loading="starting"
        :disabled="starting"
        @click="startGame"
      >
        {{ starting ? 'Building deck…' : `Start Game (${connectedCount} connected)` }}
      </n-button>
      <p v-if="startError" class="mt-3 text-sm text-red-400">
        Couldn't build the deck ({{ startError }}).
      </p>
    </div>

    <!-- Profile Maker: source selection, shown after a player connects to Spotify -->
    <n-modal
      :show="sourceModalPlayer !== null"
      preset="card"
      title="Profile Maker"
      :bordered="false"
      style="width: 90%; max-width: 30rem"
      @update:show="(v: boolean) => { if (!v) closeSourceModal() }"
    >
      <div class="flex flex-col gap-4 text-left">
        <p class="-mt-2 text-sm text-gray-400">
          We've already pulled a big slice of
          <span class="font-semibold text-white">{{ sourceModalPlayer?.username }}</span
          >'s listening — their top songs from the last year. Pick what goes into their profile.
        </p>

        <!-- Top songs — fetched on connect, but including them is a choice -->
        <div class="rounded-lg border border-neutral-800 bg-neutral-900/60 p-4">
          <n-checkbox v-model:checked="includeTopTracks">
            <span class="font-medium text-white">Top songs</span>
          </n-checkbox>
          <p class="mt-1 pl-6 text-xs text-gray-500">
            Short, medium &amp; long term — hundreds of tracks, already fetched. Untick to build
            this profile from playlists only.
          </p>
        </div>

        <!-- Playlists — real list fetched from Spotify -->
        <div class="rounded-lg border border-neutral-800 bg-neutral-900/60 p-4">
          <div class="flex items-center justify-between gap-3">
            <div>
              <p class="font-medium text-white">Add a playlist</p>
              <p class="text-xs text-gray-500">
                {{
                  includeTopTracks
                    ? "Optional — pick one if you think it'd round out the profile."
                    : 'Top songs are off, so pick at least one playlist.'
                }}
                {{ selectedPlaylists.length }} selected.
              </p>
            </div>
            <n-button size="small" ghost @click="showPlaylists = !showPlaylists">
              {{ showPlaylists ? 'Hide' : 'See playlists' }}
            </n-button>
          </div>

          <div v-if="showPlaylists" class="mt-3 max-h-48 overflow-y-auto pr-1">
            <div v-if="playlistsLoading" class="flex items-center gap-2 text-sm text-gray-400">
              <n-spin :size="16" /> Loading playlists…
            </div>
            <p v-else-if="playlistsError" class="text-sm text-red-400">
              Couldn't load playlists ({{ playlistsError }}).
            </p>
            <p v-else-if="playlists.length === 0" class="text-sm text-gray-500">
              No playlists found.
            </p>
            <n-checkbox-group v-else v-model:value="selectedPlaylists">
              <div class="flex flex-col gap-2">
                <n-checkbox v-for="pl in playlists" :key="pl.id" :value="pl.id">
                  <span class="text-white">{{ pl.name }}</span>
                </n-checkbox>
              </div>
            </n-checkbox-group>
          </div>
        </div>

        <p v-if="profileError" class="text-sm text-red-400">
          Couldn't build the profile ({{ profileError }}).
        </p>
      </div>

      <template #footer>
        <div class="flex items-center justify-end gap-3">
          <span v-if="!canBuildProfile" class="text-xs text-amber-400">
            Pick at least one source.
          </span>
          <n-button
            type="primary"
            size="large"
            :loading="buildingProfile"
            :disabled="buildingProfile || !canBuildProfile"
            @click="buildProfile"
          >
            Build profile
          </n-button>
        </div>
      </template>
    </n-modal>
  </section>
</template>
