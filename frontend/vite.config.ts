import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: false,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@docs': path.resolve(__dirname, '../docs'),
      react: path.resolve(__dirname, './node_modules/react'),
      'react-dom': path.resolve(__dirname, './node_modules/react-dom'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('react-dom') || id.includes('/react/')) {
              return 'vendor-react'
            }
            if (id.includes('lucide-react') || id.includes('framer-motion')) {
              return 'vendor-ui'
            }
            if (id.includes('zustand')) {
              return 'vendor-state'
            }
            if (id.includes('react-markdown') || id.includes('remark-') || id.includes('rehype-') || id.includes('unified') || id.includes('mdast') || id.includes('hast') || id.includes('micromark')) {
              return 'vendor-markdown'
            }
            if (id.includes('mermaid') || id.includes('dagre') || id.includes('d3') || id.includes('elkjs')) {
              return 'vendor-mermaid'
            }
          }
        },
      },
    },
  },
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': {
        target:
          process.env.VITE_PROXY_TARGET ||
          `http://127.0.0.1:${process.env.VIZ_PORT || '8001'}`,
        changeOrigin: true,
      },
    },
  },
})
