<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NButton } from 'naive-ui'
import { useRouter } from 'vue-router'

import { loadGameDeck, saveGameDeck } from '../lib/gameDeck'
import {
  buildPool,
  createSession,
  endSession,
  recordPlay,
  type PoolSong,
} from '../lib/api'
import { useSpotifyPlayer } from '../composables/useSpotifyPlayer'

interface Card {
  id: number
  spotifyId: string
  isrc: string | null
  artist: string
  title: string
  year: number
  uri: string
  albumArt: string | null
}

interface Player {
  id: number
  name: string
  deck: Card[]
  correct: number // running "guessed the year right" tally, shown on the deck
}

const router = useRouter()

// The real deck built at Start Game, dealt as a shared shuffled draw pile.
const players = ref<Player[]>([])
const drawPile = ref<Card[]>([])
const currentCard = ref<Card | null>(null)
const currentTurn = ref(0) // index into players
const viewedDeckId = ref(0)
const finished = ref(false) // draw pile exhausted
const ended = ref(false) // host pressed "End game"

// The current game's playable cards (this deck). `playedSpotifyIds` accumulates every song
// actually played, across games — on Play again the backend rebuilds the deck excluding these
// and seeding the current deck's still-unplayed songs (which then get topped back up to full).
//
// That list is also mirrored to the backend, card by card, against `sessionId` — so the group's
// history survives a refresh, a closed tab, and the next time they play together.
let allCards: Card[] = []
const playedSpotifyIds = ref<string[]>([])
const lobbyId = ref('')
const sessionId = ref<string | null>(null)
const playAgainLoading = ref(false)
const playAgainError = ref<string | null>(null)

// --- Spotify playback (never autoplays; the middle button is the only trigger) ---
const playback = useSpotifyPlayer()
const playingUri = ref<string | null>(null) // which uri the SDK is currently loaded with

function shuffle<T>(arr: T[]): T[] {
  const a = arr.slice()
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[a[i], a[j]] = [a[j], a[i]]
  }
  return a
}

onMounted(() => {
  const handoff = loadGameDeck()
  if (!handoff || handoff.players.length === 0) {
    router.replace('/start')
    return
  }
  lobbyId.value = handoff.lobbyId
  sessionId.value = handoff.sessionId ?? null
  allCards = loadCards(handoff.pool)
  players.value = handoff.players.map((p, i) => ({
    id: i,
    name: p.username,
    deck: [],
    correct: 0,
  }))
  startGameRound(allCards)

  void playback.connect(handoff.playbackToken ?? null)
})

// Only songs with a known year are playable (guess-the-year needs one). Ids are local to a game
// (deck render keys); cross-game "already played" memory keys on the stable Spotify id instead.
function loadCards(pool: PoolSong[]): Card[] {
  let idc = 0
  return pool
    .filter((s) => s.year != null && s.name)
    .map((s) => ({
      id: idc++,
      spotifyId: s.spotify_id,
      isrc: s.isrc,
      artist: s.artist ?? 'Unknown',
      title: s.name as string,
      year: s.year as number,
      uri: s.spotify_uri,
      albumArt: s.album_art,
    }))
}

function markPlayed(card: Card) {
  if (playedSpotifyIds.value.includes(card.spotifyId)) return
  playedSpotifyIds.value.push(card.spotifyId)
  // Persist it too. Fire-and-forget on purpose: the write is idempotent server-side, and a
  // failed history write must never interrupt a game in progress.
  if (sessionId.value) {
    void recordPlay(sessionId.value, {
      spotify_id: card.spotifyId,
      isrc: card.isrc,
      name: card.title,
      artist: card.artist,
      year: card.year,
    }).catch(() => {})
  }
}

/** (Re)start a game from `cards`: reshuffle, deal one anchor each, reset tallies + turn. */
function startGameRound(cards: Card[]) {
  const pile = shuffle(cards)
  for (const p of players.value) {
    // Each player starts with one anchor card in their timeline.
    p.deck = pile.length ? [pile.shift() as Card] : []
    p.correct = 0
  }
  drawPile.value = pile
  currentTurn.value = 0
  viewedDeckId.value = 0
  ended.value = false
  finished.value = false
  resetForNextCard()
  stopPlayback()
  currentCard.value = drawPile.value.shift() ?? null
  if (currentCard.value == null) finished.value = true
}

const currentTurnName = computed(() => players.value[currentTurn.value]?.name ?? '')

// Placement state machine:
//  phase 'playing'   – card sits in the middle, nothing placed
//  phase 'selecting' – card is "in hand"; deck gaps illuminate, pick one
//  phase 'placed'    – card dropped into a gap in the timeline
//  revealed          – answer is shown (persists while the card is re-placed/moved)
type Phase = 'playing' | 'selecting' | 'placed'
const phase = ref<Phase>('playing')
const revealed = ref(false)
const placedDeckId = ref<number | null>(null)
const placedIndex = ref<number | null>(null)
const showGuessPrompt = ref(false)

const viewedPlayer = computed(() =>
  players.value.find((p) => p.id === viewedDeckId.value),
)

const cardInMiddle = computed(
  () => phase.value === 'playing' || phase.value === 'selecting',
)
const inReveal = computed(() => phase.value === 'placed' && revealed.value)

// Rendered sequence for the viewed deck: real cards, illuminated gaps (while
// selecting), and the placed current card inserted at its gap.
type DeckItem =
  | { kind: 'card'; card: Card }
  | { kind: 'gap'; index: number }
  | { kind: 'placed' }

const viewedItems = computed<DeckItem[]>(() => {
  const deck = viewedPlayer.value?.deck ?? []
  const showGaps = phase.value === 'selecting'
  const placedHere =
    phase.value === 'placed' && placedDeckId.value === viewedDeckId.value

  const items: DeckItem[] = []
  for (let i = 0; i <= deck.length; i++) {
    if (placedHere && placedIndex.value === i) {
      items.push({ kind: 'placed' })
    } else if (showGaps) {
      items.push({ kind: 'gap', index: i })
    }
    if (i < deck.length) items.push({ kind: 'card', card: deck[i] })
  }
  return items
})

// --- Playback controls ---
async function togglePlay() {
  const card = currentCard.value
  if (!card) return
  if (playback.isPlaying.value) {
    await playback.pause()
    return
  }
  if (playingUri.value === card.uri) {
    await playback.resume()
  } else {
    await playback.playUri(card.uri)
    playingUri.value = card.uri
  }
}

function stopPlayback() {
  if (playback.isPlaying.value) void playback.pause()
  playingUri.value = null
}

// --- Placement actions ---
function beginPlacing() {
  if (phase.value === 'playing') phase.value = 'selecting'
}

function cancelPlacing() {
  // Return an un-revealed in-hand card to the middle. (A revealed card uses Back to middle.)
  if (revealed.value) return
  phase.value = 'playing'
  placedDeckId.value = null
  placedIndex.value = null
}

function placeInGap(index: number) {
  if (phase.value !== 'selecting') return
  placedDeckId.value = viewedDeckId.value
  placedIndex.value = index
  phase.value = 'placed'
}

function pickUpPlaced() {
  // Lift a placed card (revealed or not) to move it to a different slot.
  if (phase.value !== 'placed') return
  phase.value = 'selecting'
  placedDeckId.value = null
  placedIndex.value = null
}

function revealAnswer() {
  if (phase.value === 'placed' && !revealed.value) revealed.value = true
}

// --- Correct-guess tally ---
function toggleGuessPrompt() {
  showGuessPrompt.value = !showGuessPrompt.value
}

function markCorrect(playerId: number) {
  const pl = players.value.find((p) => p.id === playerId)
  if (pl) pl.correct += 1
  showGuessPrompt.value = false
}

// --- Reveal-state exits ---
function resetForNextCard() {
  placedDeckId.value = null
  placedIndex.value = null
  revealed.value = false
  showGuessPrompt.value = false
  phase.value = 'playing'
}

function dealNext() {
  // The card we're leaving has now been played — remember it so Play again won't redeal it.
  if (currentCard.value) markPlayed(currentCard.value)
  stopPlayback()
  currentTurn.value = (currentTurn.value + 1) % players.value.length
  viewedDeckId.value = currentTurn.value
  const next = drawPile.value.shift() ?? null
  currentCard.value = next
  resetForNextCard()
  if (next == null) finished.value = true
}

function nextCard() {
  // Commit the placed card into the timeline it was dropped in, keeping its slot.
  if (placedDeckId.value != null && placedIndex.value != null && currentCard.value) {
    const pl = players.value.find((p) => p.id === placedDeckId.value)
    pl?.deck.splice(placedIndex.value, 0, currentCard.value)
  }
  dealNext()
}

function trashCard() {
  // Discard the current card entirely — it lands in nobody's deck.
  dealNext()
}

function backToMiddle() {
  // Pull the card out of the deck and return to the now-playing view, answer hidden.
  resetForNextCard()
}

function switchDeck(id: number) {
  viewedDeckId.value = id
}

// --- End game / winner / play again ---
const gameOver = computed(() => finished.value || ended.value)

// A player's score = cards in their timeline + correct guesses. Highest wins.
const standings = computed(() =>
  players.value
    .map((p) => ({ id: p.id, name: p.name, score: p.deck.length + p.correct }))
    .sort((a, b) => b.score - a.score),
)

const winners = computed(() => {
  const s = standings.value
  if (s.length === 0) return { names: [] as string[], tie: false }
  const top = s[0].score
  const names = s.filter((x) => x.score === top).map((x) => x.name)
  return { names, tie: names.length > 1 }
})

function endGame() {
  // A revealed-but-not-advanced card was still played — count it before ending.
  if (currentCard.value && revealed.value) markPlayed(currentCard.value)
  stopPlayback()
  ended.value = true
  if (sessionId.value) void endSession(sessionId.value).catch(() => {})
}

// Play again: the backend rebuilds the deck, excluding every song played so far and seeding the
// current deck's still-unplayed songs, then tops it back up to full size via the deck algorithm.
async function playAgain() {
  playAgainLoading.value = true
  playAgainError.value = null
  try {
    const unplayed = allCards
      .filter((c) => !playedSpotifyIds.value.includes(c.spotifyId))
      .map((c) => c.spotifyId)
    const { pool } = await buildPool(lobbyId.value, {
      excludeSpotifyIds: [...playedSpotifyIds.value],
      seedSpotifyIds: unplayed,
    })
    const cards = loadCards(pool)
    if (cards.length === 0) {
      playAgainError.value = 'No more songs available to build a new deck.'
      return
    }
    // A second game is a second session — that's what makes the lobby's history list read as
    // "this game on this date" rather than one endless blob.
    if (sessionId.value) void endSession(sessionId.value).catch(() => {})
    try {
      sessionId.value = (await createSession(lobbyId.value)).session_id
    } catch {
      sessionId.value = null // history stops recording; the game still plays
    }
    // Keep the handoff in step, so a refresh mid-game resumes against the right session.
    const handoff = loadGameDeck()
    if (handoff) saveGameDeck({ ...handoff, pool, sessionId: sessionId.value })
    allCards = cards
    startGameRound(cards)
  } catch (e) {
    playAgainError.value = e instanceof Error ? e.message : String(e)
  } finally {
    playAgainLoading.value = false
  }
}
</script>

<template>
  <section class="flex min-h-screen flex-col bg-black text-white">
    <!-- Top: turn indicator + end-game -->
    <header class="relative flex items-center justify-center gap-2 border-b border-neutral-900 py-4">
      <span class="text-sm uppercase tracking-widest text-gray-500">Current turn</span>
      <span class="text-lg font-semibold text-green-400">{{ currentTurnName }}</span>
      <n-button
        v-if="!gameOver"
        size="small"
        type="error"
        ghost
        round
        class="absolute right-4"
        @click="endGame"
      >
        End game
      </n-button>
    </header>

    <!-- Middle: the now-playing card / reveal controls -->
    <div class="flex flex-1 flex-col items-center justify-center gap-5 px-6">
      <!-- Game over: winner + play again (from both "End game" and an exhausted pile) -->
      <div
        v-if="gameOver"
        class="flex w-80 flex-col items-center gap-3 rounded-2xl border border-green-700 bg-neutral-900 p-6 text-center"
      >
        <span v-if="winners.tie" class="text-2xl font-bold text-green-400">
          It's a tie! 🎉
        </span>
        <span v-else class="text-2xl font-bold text-green-400">
          Good job {{ winners.names[0] }}, you won! 🎉
        </span>
        <p v-if="winners.tie" class="text-sm text-gray-300">
          {{ winners.names.join(' & ') }} tied.
        </p>

        <!-- Final standings: cards + correct = score -->
        <ul class="w-full divide-y divide-neutral-800 text-sm">
          <li v-for="s in standings" :key="s.id" class="flex justify-between py-1.5">
            <span class="font-medium text-white">{{ s.name }}</span>
            <span class="text-gray-400">{{ s.score }} pts</span>
          </li>
        </ul>

        <n-button
          type="primary"
          size="large"
          round
          class="mt-1"
          :loading="playAgainLoading"
          :disabled="playAgainLoading"
          @click="playAgain"
        >
          {{ playAgainLoading ? 'Rebuilding deck…' : 'Play again' }}
        </n-button>
        <p v-if="playAgainError" class="text-xs text-red-400">{{ playAgainError }}</p>
        <n-button size="small" round @click="router.push('/start')">Back to lobby</n-button>
      </div>

      <template v-else>
        <!-- Playback control — present for the whole card, never autoplays -->
        <div class="flex flex-col items-center gap-1">
          <n-button
            circle
            size="large"
            type="primary"
            :disabled="!playback.ready.value"
            @click="togglePlay"
          >
            <span class="text-xl">{{ playback.isPlaying.value ? '⏸' : '▶' }}</span>
          </n-button>
          <span v-if="playback.error.value" class="max-w-xs text-center text-xs text-red-400">
            {{ playback.error.value }}
          </span>
          <span v-else-if="!playback.ready.value" class="text-xs text-gray-500">
            Connecting to Spotify…
          </span>
        </div>

        <!-- Face-down card while it's still in the middle -->
        <div
          v-if="cardInMiddle"
          class="relative flex h-60 w-44 flex-col items-center justify-center gap-3 rounded-2xl border border-neutral-700 bg-gradient-to-br from-neutral-800 to-black transition"
          :class="
            phase === 'playing'
              ? 'cursor-pointer shadow-[0_0_30px_rgba(29,185,84,0.35)] hover:shadow-[0_0_40px_rgba(29,185,84,0.6)]'
              : 'opacity-60'
          "
          @click="beginPlacing"
        >
          <span
            v-if="phase === 'playing'"
            class="pointer-events-none absolute inset-0 rounded-2xl ring-2 ring-green-500/60"
            style="animation: nowplaying 1.6s ease-in-out infinite"
          />
          <span class="animate-spin text-5xl [animation-duration:3s]">💿</span>
          <span class="text-sm font-medium tracking-wide text-green-400">
            {{ revealed ? 'Pick a new slot ↓' : 'Now Playing' }}
          </span>
        </div>

        <p v-if="phase === 'playing'" class="text-sm text-gray-500">
          Click the card, then choose a slot in the deck below.
        </p>

        <div v-else-if="phase === 'selecting'" class="flex flex-col items-center gap-3">
          <p class="text-sm text-green-400">Pick a glowing slot in the deck ↓</p>
          <n-button v-if="!revealed" size="small" tertiary @click="cancelPlacing">
            Back to middle
          </n-button>
        </div>

        <div v-else-if="phase === 'placed' && !revealed" class="flex flex-col items-center gap-3">
          <n-button type="primary" size="large" round @click="revealAnswer">
            Reveal Answer
          </n-button>
          <p class="text-xs text-gray-500">…or click the placed card to move it.</p>
        </div>

        <!-- Revealed answer + all the reveal-state options -->
        <div
          v-else-if="inReveal && currentCard"
          class="flex w-80 flex-col items-center gap-3 rounded-2xl border border-green-700 bg-neutral-900 p-5 text-center"
        >
          <span class="text-3xl font-extrabold text-green-400">{{ currentCard.year }}</span>
          <span class="text-lg font-semibold">{{ currentCard.title }}</span>
          <span class="text-sm text-gray-400">{{ currentCard.artist }}</span>

          <!-- Correctly guessed? → who -->
          <div class="w-full">
            <n-button size="small" :type="showGuessPrompt ? 'primary' : 'default'" round @click="toggleGuessPrompt">
              Correctly guessed?
            </n-button>
            <div v-if="showGuessPrompt" class="mt-3 flex flex-col items-center gap-2">
              <span class="text-xs uppercase tracking-widest text-gray-500">Who?</span>
              <div class="flex flex-wrap justify-center gap-2">
                <n-button
                  v-for="p in players"
                  :key="p.id"
                  size="small"
                  ghost
                  round
                  @click="markCorrect(p.id)"
                >
                  {{ p.name }}
                </n-button>
              </div>
            </div>
          </div>

          <p class="text-xs text-gray-500">
            Click the card in the deck to move it to another slot.
          </p>

          <!-- Exits -->
          <div class="mt-1 flex w-full items-center justify-center gap-2">
            <n-button size="small" tertiary round @click="backToMiddle">↩ Back to middle</n-button>
            <n-button size="small" type="error" ghost round @click="trashCard">🗑 Trash</n-button>
          </div>
          <n-button type="primary" size="large" round @click="nextCard">Next card</n-button>
        </div>
      </template>
    </div>

    <!-- Bottom: deck switcher + the viewed player's deck -->
    <footer class="border-t border-neutral-900 bg-neutral-950/60 px-6 pb-8 pt-4">
      <!-- Deck switcher (each chip shows that player's correct-guess tally) -->
      <div class="mb-3 flex flex-wrap items-center justify-center gap-2">
        <n-button
          v-for="p in players"
          :key="p.id"
          size="small"
          :type="p.id === viewedDeckId ? 'primary' : 'default'"
          :ghost="p.id !== viewedDeckId"
          round
          @click="switchDeck(p.id)"
        >
          {{ p.name }} · ✓{{ p.correct }}
        </n-button>
      </div>

      <!-- Viewed deck header: name (left) + correct tally (top-right of the deck) -->
      <div class="mx-auto mb-3 flex max-w-3xl items-center justify-between px-1">
        <p class="text-sm text-gray-400">
          Viewing <span class="font-semibold text-white">{{ viewedPlayer?.name }}</span
          >'s deck
        </p>
        <span
          class="rounded-full border border-green-800 bg-green-950/40 px-3 py-0.5 text-sm font-semibold text-green-400"
        >
          ✓ {{ viewedPlayer?.correct ?? 0 }} correct
        </span>
      </div>

      <!-- The deck row -->
      <ul
        v-auto-animate
        class="flex items-stretch justify-center gap-3 overflow-x-auto pb-2"
      >
        <template v-for="item in viewedItems">
          <!-- A card in the deck -->
          <li
            v-if="item.kind === 'card'"
            :key="`card-${item.card.id}`"
            class="flex h-44 w-32 shrink-0 flex-col justify-between rounded-xl border border-neutral-700 bg-neutral-900 p-3 text-left"
          >
            <span class="text-2xl font-extrabold text-green-400">{{ item.card.year }}</span>
            <div>
              <p class="line-clamp-2 text-sm font-semibold leading-tight">
                {{ item.card.title }}
              </p>
              <p class="mt-1 text-xs text-gray-400">{{ item.card.artist }}</p>
            </div>
          </li>

          <!-- An illuminated empty slot to drop into -->
          <li
            v-else-if="item.kind === 'gap'"
            :key="`gap-${item.index}`"
            class="flex h-44 w-32 shrink-0 cursor-pointer items-center justify-center rounded-xl border-2 border-dashed border-green-500 text-green-400 shadow-[0_0_20px_rgba(29,185,84,0.5)] transition hover:bg-green-500/10"
            style="animation: slotpulse 1.4s ease-in-out infinite"
            @click="placeInGap(item.index)"
          >
            <span class="text-3xl leading-none">+</span>
          </li>

          <!-- The current card, placed in the timeline (movable; flips when revealed) -->
          <li
            v-else
            key="placed"
            class="flex h-44 w-32 shrink-0 cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-green-600 bg-gradient-to-br from-neutral-800 to-black shadow-[0_0_20px_rgba(29,185,84,0.5)] transition hover:shadow-[0_0_28px_rgba(29,185,84,0.75)]"
            @click="pickUpPlaced"
          >
            <template v-if="revealed && currentCard">
              <span class="px-3 text-center">
                <span class="block text-xl font-extrabold text-green-400">{{ currentCard.year }}</span>
                <span class="mt-1 block text-xs font-semibold leading-tight">{{ currentCard.title }}</span>
                <span class="mt-0.5 block text-[11px] text-gray-400">{{ currentCard.artist }}</span>
              </span>
            </template>
            <template v-else>
              <span class="animate-spin text-3xl [animation-duration:3s]">💿</span>
              <span class="text-[11px] text-gray-400">tap to move</span>
            </template>
          </li>
        </template>
      </ul>
    </footer>
  </section>
</template>

<style scoped>
@keyframes nowplaying {
  0%,
  100% {
    opacity: 0.4;
    transform: scale(1);
  }
  50% {
    opacity: 1;
    transform: scale(1.03);
  }
}
@keyframes slotpulse {
  0%,
  100% {
    box-shadow: 0 0 12px rgba(29, 185, 84, 0.35);
  }
  50% {
    box-shadow: 0 0 24px rgba(29, 185, 84, 0.7);
  }
}
</style>
