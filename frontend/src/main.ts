import { createApp } from 'vue'
import { autoAnimatePlugin } from '@formkit/auto-animate/vue'
import { router } from './router'
import './style.css'
import App from './App.vue'

// Naive UI needs no global registration — components are imported where used and
// tree-shaken. autoAnimatePlugin registers the `v-auto-animate` directive globally.
createApp(App).use(router).use(autoAnimatePlugin).mount('#app')
