import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    // Reachable from outside the container (Docker), Vite equivalent of -H 0.0.0.0.
    host: true,
    port: 5173,
    watch: {
      // Docker on Windows/macOS runs in a VM where inotify doesn't cross the
      // boundary; poll instead so host-edited files still trigger hot-reload.
      usePolling: true,
    },
  },
})
