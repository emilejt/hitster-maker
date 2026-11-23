<script setup lang="ts">
import { NConfigProvider, darkTheme } from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'

// Spotify-green primary across all Naive UI components.
const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#1DB954',
    primaryColorHover: '#1ED760',
    primaryColorPressed: '#1AA34A',
    primaryColorSuppl: '#1ED760',
  },
}
</script>

<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides">
    <router-view />
  </n-config-provider>
</template>
