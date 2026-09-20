import { defineConfig } from '@playwright/test'

const port = Number(process.env.AUDIO_MEMORY_PLAYWRIGHT_PORT || 4173)
const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 20_000,
  use: {
    baseURL,
    channel: 'chrome',
    headless: true,
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    env: {
      AUDIO_MEMORY_EXPECTED_PROFILE_OVERRIDE: '',
    },
  },
})
