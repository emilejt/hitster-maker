<script setup lang="ts">
// Dev-facing tuner for the deck builder's embedding tiers. Connect a player (same OAuth +
// Profile Maker as the lobby, via usePlayerLobby), then see how their library splits into
// taste clusters and which popularity-ranked neighbours each centroid pulls. With two players,
// compare their clusters and blend them (the tier-3 mechanic).
import { computed, reactive, ref, watch } from 'vue'
import {
  NButton,
  NCheckbox,
  NCheckboxGroup,
  NInput,
  NInputNumber,
  NModal,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSlider,
  NSpin,
} from 'naive-ui'
import type { InputInst } from 'naive-ui'
import { RouterLink } from 'vue-router'

import { usePlayerLobby, type Player } from '../composables/usePlayerLobby'
import { clearLobbyState } from '../lib/lobbyState'
import {
  getClusters,
  getNeighbors,
  getProximity,
  combineClusters,
  getNearestSeeds,
  type ClusterMode,
  type ClustersResponse,
  type NeighborsResponse,
  type ProximityResponse,
  type CombineResponse,
  type NearestSeedsResponse,
} from '../lib/analysisApi'

const {
  lobbyId,
  lobbyError,
  players,
  canAddMore,
  addPlayer: addLobbyPlayer,
  connectSpotify,
  sourceModalPlayer,
  showPlaylists,
  likedCount,
  selectedPlaylists,
  playlists,
  playlistsLoading,
  playlistsError,
  buildingProfile,
  profileError,
  closeSourceModal,
  buildProfile,
} = usePlayerLobby()

const username = ref('')
const usernameInput = ref<InputInst | null>(null)

const K_MIN = 1
const K_MAX = 6
const N_MAX = 200
const DIST_MIN = 0.05
const DIST_MAX = 0.95
const DIST_STEP = 0.05

// Global clustering choice for the whole tool. `method` + `distance` + `minClusterSize` apply
// everywhere; the per-player `k` (below) only matters for k-means. Agglomerative uses the single
// global cut and drops clusters below the minimum size.
const clustering = reactive<{
  method: ClusterMode['method']
  distance: number
  minClusterSize: number
}>({
  method: 'kmeans',
  distance: 0.6,
  minClusterSize: 3,
})
const clusterMode = computed<ClusterMode>(() => ({
  method: clustering.method,
  distance: clustering.distance,
  minClusterSize: clustering.minClusterSize,
}))
const isAgglo = computed(() => clustering.method === 'agglomerative')

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function addPlayer() {
  const created = addLobbyPlayer(username.value)
  if (!created) return
  username.value = ''
  usernameInput.value?.focus()
}

// --- Per-player analysis state ------------------------------------------------

interface AnalysisState {
  k: number
  clusters: ClustersResponse | null
  loadingClusters: boolean
  clustersError: string | null
  n: number
  neighbors: NeighborsResponse | null
  loadingNeighbors: boolean
  neighborsError: string | null
}

const analysis = reactive<Record<string, AnalysisState>>({})

const connectedPlayers = computed(() =>
  players.value.filter((p) => p.status === 'connected'),
)

// Paired list so the template never has to index the record (keeps it undefined-safe).
const analyzed = computed(() =>
  connectedPlayers.value
    .map((player) => ({ player, state: analysis[player.id] }))
    .filter((x): x is { player: Player; state: AnalysisState } => !!x.state),
)

// Spin up analysis state + an initial cluster load the moment a player becomes connected.
watch(
  connectedPlayers,
  (list) => {
    for (const p of list) {
      if (!analysis[p.id]) {
        analysis[p.id] = {
          k: 3,
          clusters: null,
          loadingClusters: false,
          clustersError: null,
          n: 50,
          neighbors: null,
          loadingNeighbors: false,
          neighborsError: null,
        }
        void loadClusters(p.id)
      }
    }
  },
  { immediate: true },
)

async function loadClusters(playerId: string) {
  const st = analysis[playerId]
  if (!st) return
  st.loadingClusters = true
  st.clustersError = null
  st.neighbors = null // clustering changed → old neighbours are stale
  try {
    st.clusters = await getClusters(lobbyId.value, playerId, st.k, clusterMode.value)
  } catch (e) {
    st.clustersError = errMsg(e)
  } finally {
    st.loadingClusters = false
  }
}

function setK(playerId: string, k: number) {
  const st = analysis[playerId]
  if (!st) return
  st.k = k
  void loadClusters(playerId)
}

function reclusterAll() {
  for (const p of connectedPlayers.value) void loadClusters(p.id)
}

// Switching method re-clusters everyone immediately; dragging the cut is debounced so the
// slider stays smooth (each recluster is a cheap, no-Spotify call, but one per drag tick adds up).
watch(() => clustering.method, reclusterAll)
let distTimer: ReturnType<typeof setTimeout> | null = null
function scheduleRecluster() {
  if (!isAgglo.value) return
  if (distTimer) clearTimeout(distTimer)
  distTimer = setTimeout(reclusterAll, 200)
}
watch(() => clustering.distance, scheduleRecluster)
watch(() => clustering.minClusterSize, scheduleRecluster)

async function loadNeighbors(playerId: string) {
  const st = analysis[playerId]
  if (!st) return
  st.loadingNeighbors = true
  st.neighborsError = null
  try {
    st.neighbors = await getNeighbors(lobbyId.value, playerId, st.k, st.n, clusterMode.value)
  } catch (e) {
    st.neighborsError = errMsg(e)
  } finally {
    st.loadingNeighbors = false
  }
}

function neighborsForCluster(state: AnalysisState, index: number) {
  return state.neighbors?.clusters.find((c) => c.index === index)?.neighbors ?? []
}

/** Bar width for a 0–1 value (similarity / popularity). */
function pct(x: number): string {
  return `${Math.round(Math.max(0, Math.min(1, x)) * 100)}%`
}

// --- 2-player analysis (F3): proximity + combine + nearest-seeds --------------

const two = reactive({
  a: '',
  b: '',
  kA: 3,
  kB: 3,
  // proximity
  proximity: null as ProximityResponse | null,
  loadingProx: false,
  proxError: null as string | null,
  // combine
  aCluster: 0,
  bCluster: 0,
  combineN: 50,
  combine: null as CombineResponse | null,
  loadingCombine: false,
  combineError: null as string | null,
  // nearest-seeds
  baseIsA: true, // role: base = A & candidate = B, or swapped
  baseCluster: 0,
  candCluster: 0,
  seedsN: 20,
  nearest: null as NearestSeedsResponse | null,
  loadingNearest: false,
  nearestError: null as string | null,
})

const canTwoPlayer = computed(() => connectedPlayers.value.length >= 2)

const playerOptions = computed(() =>
  connectedPlayers.value.map((p) => ({ label: p.username, value: p.id })),
)

function nameOf(id: string): string {
  return connectedPlayers.value.find((p) => p.id === id)?.username ?? id
}

// Default A/B to the first two connected players once they exist.
watch(
  connectedPlayers,
  (list) => {
    if (list.length < 2) return
    if (!two.a || !list.some((p) => p.id === two.a)) two.a = list[0].id
    if (!two.b || two.b === two.a || !list.some((p) => p.id === two.b)) {
      two.b = list.find((p) => p.id !== two.a)?.id ?? ''
    }
  },
  { immediate: true },
)

// Effective cluster counts come from the proximity response once loaded; before that, fall
// back to the requested k so the cluster dropdowns still populate.
const kAEff = computed(() => two.proximity?.k_a ?? two.kA)
const kBEff = computed(() => two.proximity?.k_b ?? two.kB)

function clusterOptions(count: number) {
  return Array.from({ length: Math.max(0, count) }, (_, i) => ({
    label: `Cluster ${i}`,
    value: i,
  }))
}

// Nearest-seeds role wiring: base provides the centroid, candidate provides the songs.
const basePlayerId = computed(() => (two.baseIsA ? two.a : two.b))
const candPlayerId = computed(() => (two.baseIsA ? two.b : two.a))
const baseK = computed(() => (two.baseIsA ? two.kA : two.kB))
const candK = computed(() => (two.baseIsA ? two.kB : two.kA))
const baseKEff = computed(() => (two.baseIsA ? kAEff.value : kBEff.value))
const candKEff = computed(() => (two.baseIsA ? kBEff.value : kAEff.value))

async function loadProximity() {
  if (!two.a || !two.b) return
  two.loadingProx = true
  two.proxError = null
  try {
    two.proximity = await getProximity(
      lobbyId.value, two.a, two.b, two.kA, two.kB, clusterMode.value,
    )
  } catch (e) {
    two.proxError = errMsg(e)
  } finally {
    two.loadingProx = false
  }
}

async function loadCombine() {
  two.loadingCombine = true
  two.combineError = null
  try {
    two.combine = await combineClusters(lobbyId.value, {
      a: two.a,
      a_cluster: two.aCluster,
      k_a: two.kA,
      b: two.b,
      b_cluster: two.bCluster,
      k_b: two.kB,
      n: two.combineN,
      method: clustering.method,
      distance: clustering.distance,
      min_cluster_size: clustering.minClusterSize,
    })
  } catch (e) {
    two.combineError = errMsg(e)
  } finally {
    two.loadingCombine = false
  }
}

async function loadNearest() {
  two.loadingNearest = true
  two.nearestError = null
  try {
    two.nearest = await getNearestSeeds(lobbyId.value, {
      base: basePlayerId.value,
      base_cluster: two.baseCluster,
      k_base: baseK.value,
      candidate: candPlayerId.value,
      candidate_cluster: two.candCluster,
      k_candidate: candK.value,
      n: two.seedsN,
      method: clustering.method,
      distance: clustering.distance,
      min_cluster_size: clustering.minClusterSize,
    })
  } catch (e) {
    two.nearestError = errMsg(e)
  } finally {
    two.loadingNearest = false
  }
}

function swapRoles() {
  two.baseIsA = !two.baseIsA
  two.baseCluster = 0
  two.candCluster = 0
  two.nearest = null
}

/** Clear the persisted lobby and reload — handy when the backend was restarted and the
 * stored lobby id is stale (clusters would 404). */
function resetAll() {
  clearLobbyState()
  window.location.reload()
}
</script>

<template>
  <section class="min-h-screen bg-black px-6 py-10 text-white">
    <header class="mx-auto mb-8 flex max-w-5xl flex-wrap items-center justify-between gap-4">
      <div>
        <h1 class="text-3xl font-extrabold tracking-tight">Taste Analysis</h1>
        <p class="text-sm text-gray-400">
          Dev tool — see how the fallback clusters a player and what each cluster generates.
        </p>
      </div>
      <div class="flex items-center gap-4">
        <n-button size="small" ghost @click="resetAll">Reset</n-button>
        <RouterLink to="/" class="text-sm text-gray-400 hover:text-white">← Home</RouterLink>
      </div>
    </header>

    <p
      v-if="lobbyError"
      class="mx-auto mb-6 max-w-5xl rounded-lg border border-red-900 bg-red-950/50 px-4 py-2 text-sm text-red-300"
    >
      {{ lobbyError }}
    </p>

    <!-- Connect players -->
    <div class="mx-auto mb-10 max-w-5xl">
      <div class="flex flex-wrap items-center gap-3">
        <n-input
          ref="usernameInput"
          v-model:value="username"
          placeholder="Player name"
          size="large"
          :maxlength="24"
          :disabled="!canAddMore"
          style="max-width: 16rem"
          @keyup.enter="addPlayer"
        />
        <n-button
          type="primary"
          size="large"
          :disabled="!username.trim() || !canAddMore"
          @click="addPlayer"
        >
          Add player
        </n-button>
      </div>

      <ul class="mt-4 flex flex-wrap gap-3">
        <li
          v-for="player in players"
          :key="player.id"
          class="flex w-48 flex-col items-center gap-2 rounded-xl border border-neutral-800 bg-neutral-900 p-4"
        >
          <span class="max-w-full truncate font-semibold">{{ player.username }}</span>

          <span v-if="player.busy" class="flex items-center gap-2 text-xs text-gray-400">
            <n-spin :size="14" /> Fetching top songs…
          </span>

          <template v-else-if="player.status === 'connected'">
            <span class="text-sm font-medium text-green-400">Connected</span>
            <span v-if="player.summary" class="text-xs text-gray-500">
              {{ player.summary.track_count }} songs
            </span>
          </template>

          <template v-else-if="player.status === 'ready'">
            <n-button size="small" type="primary" ghost round @click="connectSpotify(player)">
              Build profile
            </n-button>
          </template>

          <n-button v-else size="small" type="primary" round @click="connectSpotify(player)">
            Connect Spotify
          </n-button>

          <span v-if="player.error" class="text-xs text-red-400">{{ player.error }}</span>
        </li>
      </ul>
    </div>

    <!-- Global clustering method (applies to every panel below) -->
    <div
      v-if="analyzed.length > 0"
      class="mx-auto mb-8 flex max-w-5xl flex-wrap items-center gap-4 rounded-xl border border-neutral-800 bg-neutral-900 px-5 py-4"
    >
      <span class="text-sm font-semibold text-gray-300">Clustering</span>
      <n-radio-group v-model:value="clustering.method" size="small">
        <n-radio-button value="kmeans">K-means (k)</n-radio-button>
        <n-radio-button value="agglomerative">Agglomerative (cut)</n-radio-button>
      </n-radio-group>

      <template v-if="isAgglo">
        <div class="flex flex-1 items-center gap-3" style="min-width: 14rem">
          <span class="shrink-0 text-sm text-gray-400">Cosine-distance cut</span>
          <n-slider
            v-model:value="clustering.distance"
            :min="DIST_MIN"
            :max="DIST_MAX"
            :step="DIST_STEP"
            style="max-width: 16rem"
          />
          <span class="w-10 shrink-0 text-right text-sm tabular-nums text-white">
            {{ clustering.distance.toFixed(2) }}
          </span>
        </div>
        <label class="flex shrink-0 items-center gap-2 text-sm text-gray-400">
          Min songs/cluster
          <n-input-number
            v-model:value="clustering.minClusterSize"
            :min="1"
            :max="50"
            size="small"
            style="width: 6rem"
          />
        </label>
      </template>
      <p v-else class="text-xs text-gray-500">
        Per-player k below. Switch to agglomerative to tune one global distance cut instead.
      </p>
    </div>

    <!-- Per-player analysis -->
    <p
      v-if="analyzed.length === 0"
      class="mx-auto max-w-5xl text-center text-gray-500"
    >
      Connect a player and build their profile to see their taste clusters.
    </p>

    <div
      v-for="{ player, state } in analyzed"
      :key="player.id"
      class="mx-auto mb-12 max-w-5xl rounded-2xl border border-neutral-800 bg-neutral-950 p-6"
    >
      <div class="mb-4 flex flex-wrap items-center justify-between gap-4">
        <h2 class="text-xl font-bold">{{ player.username }}</h2>
        <div v-if="!isAgglo" class="flex items-center gap-2 text-sm">
          <span class="text-gray-400">Clusters (k)</span>
          <n-input-number
            :value="state.k"
            :min="K_MIN"
            :max="K_MAX"
            size="small"
            style="width: 6rem"
            @update:value="(v: number | null) => v !== null && setK(player.id, v)"
          />
        </div>
        <div v-else class="text-sm text-gray-400">
          cut {{ clustering.distance.toFixed(2) }} · min {{ clustering.minClusterSize }} →
          <span class="font-semibold text-white">{{ state.clusters?.k ?? '…' }}</span> clusters
          <span v-if="state.clusters?.dismissed" class="text-gray-500">
            ({{ state.clusters.dismissed }} dismissed)
          </span>
        </div>
      </div>

      <div v-if="state.loadingClusters" class="flex items-center gap-2 text-gray-400">
        <n-spin :size="16" /> Clustering…
      </div>
      <p v-else-if="state.clustersError" class="text-sm text-red-400">
        {{ state.clustersError }}
      </p>

      <template v-else-if="state.clusters">
        <p class="mb-4 text-xs text-gray-500">
          {{ state.clusters.seeds_in_model }} / {{ state.clusters.seeds_total }} seeds in model
          <span v-if="state.clusters.silhouette !== null">
            · silhouette {{ state.clusters.silhouette }}</span
          >
        </p>

        <!-- Neighbour controls -->
        <div class="mb-6 flex flex-wrap items-center gap-3">
          <span class="text-sm text-gray-400">Neighbours per cluster (n)</span>
          <n-input-number
            v-model:value="state.n"
            :min="1"
            :max="N_MAX"
            :step="10"
            size="small"
            style="width: 7rem"
          />
          <n-button
            size="small"
            type="primary"
            :loading="state.loadingNeighbors"
            @click="loadNeighbors(player.id)"
          >
            Generate neighbours
          </n-button>
          <span v-if="state.neighborsError" class="text-xs text-red-400">
            {{ state.neighborsError }}
          </span>
        </div>

        <div class="flex flex-wrap gap-4">
          <div
            v-for="cluster in state.clusters.clusters"
            :key="cluster.index"
            class="w-72 flex-1 rounded-xl border border-neutral-800 bg-neutral-900 p-4"
          >
            <div class="mb-3 flex items-center justify-between">
              <span class="font-semibold">Cluster {{ cluster.index }}</span>
              <span class="text-xs text-gray-500">{{ cluster.size }} songs</span>
            </div>

            <!-- Seed songs -->
            <ul class="flex flex-col gap-2">
              <li v-for="song in cluster.songs" :key="song.mbid" class="text-xs">
                <div class="flex justify-between gap-2">
                  <span class="truncate text-white">{{ song.name ?? song.mbid }}</span>
                  <span class="shrink-0 text-gray-500">{{ song.similarity }}</span>
                </div>
                <div class="truncate text-gray-500">
                  {{ song.artist }}<span v-if="song.year"> · {{ song.year }}</span>
                </div>
                <div class="mt-1 h-1 rounded bg-neutral-800">
                  <div class="h-1 rounded bg-green-600" :style="{ width: pct(song.similarity) }" />
                </div>
              </li>
            </ul>

            <!-- Generated neighbours -->
            <template v-if="neighborsForCluster(state, cluster.index).length">
              <p class="mt-4 mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                Generates
              </p>
              <ul class="flex flex-col gap-2">
                <li
                  v-for="nb in neighborsForCluster(state, cluster.index)"
                  :key="nb.mbid"
                  class="text-xs"
                >
                  <div class="flex justify-between gap-2">
                    <span class="truncate text-emerald-200">{{ nb.name ?? nb.mbid }}</span>
                    <span class="shrink-0 text-gray-500">{{ nb.popularity }}</span>
                  </div>
                  <div class="truncate text-gray-500">
                    {{ nb.artist }}<span v-if="nb.year"> · {{ nb.year }}</span>
                  </div>
                  <div class="mt-1 h-1 rounded bg-neutral-800">
                    <div class="h-1 rounded bg-emerald-500" :style="{ width: pct(nb.popularity) }" />
                  </div>
                </li>
              </ul>
            </template>
          </div>
        </div>
      </template>
    </div>

    <!-- Two-player analysis (F3) -->
    <div
      v-if="canTwoPlayer"
      class="mx-auto mb-12 max-w-5xl rounded-2xl border border-neutral-800 bg-neutral-950 p-6"
    >
      <h2 class="mb-1 text-xl font-bold">Two-player analysis</h2>
      <p class="mb-5 text-sm text-gray-400">
        Cross-player overlap, common-ground generation, and each player's real songs closest
        to the other's taste.
      </p>

      <!-- Player + k pickers, shared by all three panels -->
      <div class="mb-6 flex flex-wrap items-end gap-4">
        <label class="flex flex-col gap-1 text-xs text-gray-400">
          Player A
          <n-select
            v-model:value="two.a"
            :options="playerOptions"
            size="small"
            style="width: 11rem"
          />
        </label>
        <label v-if="!isAgglo" class="flex flex-col gap-1 text-xs text-gray-400">
          k (A)
          <n-input-number v-model:value="two.kA" :min="K_MIN" :max="K_MAX" size="small" style="width: 6rem" />
        </label>
        <label class="flex flex-col gap-1 text-xs text-gray-400">
          Player B
          <n-select
            v-model:value="two.b"
            :options="playerOptions"
            size="small"
            style="width: 11rem"
          />
        </label>
        <label v-if="!isAgglo" class="flex flex-col gap-1 text-xs text-gray-400">
          k (B)
          <n-input-number v-model:value="two.kB" :min="K_MIN" :max="K_MAX" size="small" style="width: 6rem" />
        </label>
        <span v-if="isAgglo" class="pb-1 text-xs text-gray-500">
          global cut {{ clustering.distance.toFixed(2) }}
        </span>
      </div>

      <!-- Proximity -->
      <div class="mb-8 rounded-xl border border-neutral-800 bg-neutral-900 p-5">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 class="font-semibold">Proximity</h3>
            <p class="text-xs text-gray-500">Every A-cluster × B-cluster closeness, strongest first.</p>
          </div>
          <n-button size="small" type="primary" :loading="two.loadingProx" @click="loadProximity">
            Compute proximity
          </n-button>
        </div>
        <p v-if="two.proxError" class="text-sm text-red-400">{{ two.proxError }}</p>
        <ul v-else-if="two.proximity" class="flex flex-col gap-2">
          <li v-for="p in two.proximity.pairs" :key="`${p.a_cluster}-${p.b_cluster}`" class="text-xs">
            <div class="flex justify-between gap-2">
              <span class="text-gray-300">A {{ p.a_cluster }} ↔ B {{ p.b_cluster }}</span>
              <span class="shrink-0 text-gray-500">{{ p.similarity }}</span>
            </div>
            <div class="mt-1 h-1 rounded bg-neutral-800">
              <div class="h-1 rounded bg-sky-500" :style="{ width: pct(p.similarity) }" />
            </div>
          </li>
        </ul>
      </div>

      <!-- Combine -->
      <div class="mb-8 rounded-xl border border-neutral-800 bg-neutral-900 p-5">
        <div class="mb-3">
          <h3 class="font-semibold">Combine (common ground)</h3>
          <p class="text-xs text-gray-500">
            Blend one cluster from each player and see what the shared space generates.
          </p>
        </div>
        <div class="mb-4 flex flex-wrap items-end gap-4">
          <label class="flex flex-col gap-1 text-xs text-gray-400">
            {{ nameOf(two.a) }} cluster
            <n-select v-model:value="two.aCluster" :options="clusterOptions(kAEff)" size="small" style="width: 9rem" />
          </label>
          <label class="flex flex-col gap-1 text-xs text-gray-400">
            {{ nameOf(two.b) }} cluster
            <n-select v-model:value="two.bCluster" :options="clusterOptions(kBEff)" size="small" style="width: 9rem" />
          </label>
          <label class="flex flex-col gap-1 text-xs text-gray-400">
            Neighbours (n)
            <n-input-number v-model:value="two.combineN" :min="1" :max="N_MAX" :step="10" size="small" style="width: 7rem" />
          </label>
          <n-button size="small" type="primary" :loading="two.loadingCombine" @click="loadCombine">
            Combine
          </n-button>
        </div>
        <p v-if="two.combineError" class="text-sm text-red-400">{{ two.combineError }}</p>
        <ul v-else-if="two.combine" class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <li v-for="nb in two.combine.neighbors" :key="nb.mbid" class="text-xs">
            <div class="flex justify-between gap-2">
              <span class="truncate text-emerald-200">{{ nb.name ?? nb.mbid }}</span>
              <span class="shrink-0 text-gray-500">{{ nb.popularity }}</span>
            </div>
            <div class="truncate text-gray-500">
              {{ nb.artist }}<span v-if="nb.year"> · {{ nb.year }}</span>
            </div>
            <div class="mt-1 h-1 rounded bg-neutral-800">
              <div class="h-1 rounded bg-emerald-500" :style="{ width: pct(nb.popularity) }" />
            </div>
          </li>
        </ul>
      </div>

      <!-- Nearest seeds -->
      <div class="rounded-xl border border-neutral-800 bg-neutral-900 p-5">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 class="font-semibold">Nearest seeds</h3>
            <p class="text-xs text-gray-500">
              The <span class="text-white">candidate</span>'s own songs closest to the
              <span class="text-white">base</span>'s taste zone. Directional.
            </p>
          </div>
          <n-button size="small" ghost @click="swapRoles">Swap roles ⇄</n-button>
        </div>

        <div class="mb-4 flex flex-wrap items-end gap-4">
          <label class="flex flex-col gap-1 text-xs text-gray-400">
            Base — {{ nameOf(basePlayerId) }} (centroid)
            <n-select v-model:value="two.baseCluster" :options="clusterOptions(baseKEff)" size="small" style="width: 9rem" />
          </label>
          <label class="flex flex-col gap-1 text-xs text-gray-400">
            Candidate — {{ nameOf(candPlayerId) }} (songs)
            <n-select v-model:value="two.candCluster" :options="clusterOptions(candKEff)" size="small" style="width: 9rem" />
          </label>
          <label class="flex flex-col gap-1 text-xs text-gray-400">
            Songs (n)
            <n-input-number v-model:value="two.seedsN" :min="1" :max="N_MAX" :step="5" size="small" style="width: 7rem" />
          </label>
          <n-button size="small" type="primary" :loading="two.loadingNearest" @click="loadNearest">
            Find nearest
          </n-button>
        </div>
        <p v-if="two.nearestError" class="text-sm text-red-400">{{ two.nearestError }}</p>
        <ul v-else-if="two.nearest" class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <li v-for="song in two.nearest.songs" :key="song.mbid" class="text-xs">
            <div class="flex justify-between gap-2">
              <span class="truncate text-white">{{ song.name ?? song.mbid }}</span>
              <span class="shrink-0 text-gray-500">{{ song.similarity }}</span>
            </div>
            <div class="truncate text-gray-500">
              {{ song.artist }}<span v-if="song.year"> · {{ song.year }}</span>
            </div>
            <div class="mt-1 h-1 rounded bg-neutral-800">
              <div class="h-1 rounded bg-green-600" :style="{ width: pct(song.similarity) }" />
            </div>
          </li>
        </ul>
      </div>
    </div>

    <!-- Profile Maker: identical to the lobby's (bound to the same composable) -->
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
          Choose how we build
          <span class="font-semibold text-white">{{ sourceModalPlayer?.username }}</span
          >'s taste.
        </p>

        <div class="rounded-lg border border-neutral-800 bg-neutral-900/60 p-4">
          <n-checkbox :checked="true" disabled>
            <span class="font-medium text-white">Top songs</span>
          </n-checkbox>
          <p class="mt-1 pl-6 text-xs text-gray-500">
            Short, medium &amp; long term — already fetched.
          </p>
        </div>

        <div class="rounded-lg border border-neutral-800 bg-neutral-900/60 p-4">
          <p class="font-medium text-white">Liked songs</p>
          <p class="mb-2 text-xs text-gray-500">
            How many of your most recently liked songs should we include?
          </p>
          <n-input-number
            v-model:value="likedCount"
            :min="0"
            :max="500"
            :step="25"
            style="width: 10rem"
          />
        </div>

        <div class="rounded-lg border border-neutral-800 bg-neutral-900/60 p-4">
          <div class="flex items-center justify-between gap-3">
            <div>
              <p class="font-medium text-white">Playlists to include</p>
              <p class="text-xs text-gray-500">{{ selectedPlaylists.length }} selected</p>
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
        <div class="flex justify-end">
          <n-button
            type="primary"
            size="large"
            :loading="buildingProfile"
            :disabled="buildingProfile"
            @click="buildProfile"
          >
            Build profile
          </n-button>
        </div>
      </template>
    </n-modal>
  </section>
</template>
