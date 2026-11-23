import { createRouter, createWebHistory } from 'vue-router'
import WelcomeView from '../views/WelcomeView.vue'
import StartAGameView from '../views/StartAGameView.vue'
import GameView from '../views/GameView.vue'
import SpotifyCallbackView from '../views/SpotifyCallbackView.vue'
import TasteAnalysisView from '../views/TasteAnalysisView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'welcome', component: WelcomeView },
    { path: '/start', name: 'start', component: StartAGameView },
    { path: '/game', name: 'game', component: GameView },
    // Dev-facing tool used to tune the deck builder's embedding tiers.
    { path: '/analysis', name: 'analysis', component: TasteAnalysisView },
    // Post-OAuth landing route — the backend redirects here (see routers/auth.py).
    { path: '/spotify', name: 'spotify-callback', component: SpotifyCallbackView },
  ],
})
