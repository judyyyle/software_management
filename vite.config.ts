import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import { mars3dPlugin } from 'vite-plugin-mars3d'
import type { ConfigEnv, UserConfig } from 'vite'
import { loadEnv } from 'vite'
import { svgBuilder } from '/@/components/icon/svg/index'
import { customHotUpdate, isProd } from '/@/utils/vite'
import { createMockMiddleware } from './mock/index'

const pathResolve = (dir: string): any => {
    return resolve(__dirname, '.', dir)
}

// https://vitejs.cn/config/
const viteConfig = ({ mode }: ConfigEnv): UserConfig => {
    const { VITE_PORT, VITE_OPEN, VITE_BASE_PATH, VITE_OUT_DIR } = loadEnv(mode, process.cwd())

    const alias: Record<string, string> = {
        '/@': pathResolve('./src/'),
        assets: pathResolve('./src/assets'),
        'vue-i18n': isProd(mode) ? 'vue-i18n/dist/vue-i18n.cjs.prod.js' : 'vue-i18n/dist/vue-i18n.cjs.js',
    }

    return {
        plugins: [
            vue(),
            svgBuilder('./src/assets/icons/'),
            customHotUpdate(),
            mars3dPlugin(),
            // 开发环境 Mock 插件，拦截后端API请求返回模拟数据
            {
                name: 'mock-server',
                configureServer(server) {
                    server.middlewares.use(createMockMiddleware())
                },
            },
        ],
        root: process.cwd(),
        resolve: { alias },
        base: VITE_BASE_PATH,
        server: {
            port: parseInt(VITE_PORT),
            open: VITE_OPEN != 'false',
            host: '0.0.0.0',
            // 开发环境通过 Mock 中间件拦截请求，无需代理到真实后端
        },
        optimizeDeps: {
            // exclude: ['mars3d-cesium'],
        },
        build: {
            cssCodeSplit: true,
            sourcemap: false,
            outDir: VITE_OUT_DIR,
            emptyOutDir: true,
            chunkSizeWarningLimit: 1500,
            rollupOptions: {
                output: {
                    manualChunks: {
                        // 分包配置，配置完成自动按需加载
                        vue: ['vue', 'vue-router', 'pinia', 'vue-i18n', 'element-plus'],
                        'mars3d-vendor': ['mars3d'],
                    },
                },
            },
        },
    }
}

export default viteConfig
