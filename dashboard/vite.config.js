import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { viteSingleFile } from 'vite-plugin-singlefile'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))

// dev 专用：把 dashboard/data.dev.json 挂到 /data.json。
// 只在 `npm run dev` 生效（configureServer）；`npm run build` 不读它，
// 产物里的数据由 server.py 注入 __CMB_DATA_B64__，与该文件无关。
function devDataPlugin() {
  return {
    name: 'cmb-dev-data',
    configureServer(server) {
      server.middlewares.use('/data.json', (_req, res) => {
        try {
          const buf = readFileSync(resolve(root, 'data.dev.json'))
          res.setHeader('Content-Type', 'application/json; charset=utf-8')
          res.end(buf)
        } catch (e) {
          res.statusCode = 404
          res.setHeader('Content-Type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({
            error: String(e.message),
            hint: 'dev 模式下需要 dashboard/data.dev.json（不入库）。先跑 README「二、Vue3 + ECharts 面板 → 开发」里那条 python 命令生成它。'
          }))
        }
      })
    }
  }
}

// 产物必须是一个自包含 HTML：MCP App 的 ui:// 资源只能返回单段 text/html，
// 不允许引用外部 js/css，所以全部内联。
export default defineConfig({
  plugins: [vue(), devDataPlugin(), viteSingleFile()],
  publicDir: false,
  build: {
    target: 'esnext',
    assetsInlineLimit: 1024 * 1024 * 32,
    cssCodeSplit: false,
    chunkSizeWarningLimit: 4096,
    rollupOptions: {
      output: { inlineDynamicImports: true }
    }
  },
  server: { port: 5273, strictPort: false }
})
