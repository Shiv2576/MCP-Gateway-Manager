import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, /api goes to the gateway so the session cookie stays same-origin.
// In the cluster, nginx does the same job (see nginx.conf).
const gateway = process.env.GATEWAY_URL || 'http://gateway.localhost'

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': { target: gateway, changeOrigin: true } } },
})
