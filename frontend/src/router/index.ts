import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '@/layouts/AppLayout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 根路径重定向到调度大屏
    { path: '/', redirect: '/dashboard' },

    // 旧路径向后兼容重定向
    { path: '/geo',      redirect: '/simulation' },
    { path: '/monitor',  redirect: '/dispatch'   },
    { path: '/analytics',redirect: '/dashboard'  },

    // ── 主布局（含侧边导航栏）────────────────────────────────────────
    {
      path: '/',
      component: AppLayout,
      children: [
        {
          path: 'dashboard',
          component: () => import('@/views/Dashboard/index.vue'),
          meta: { title: '调度性能大屏 · HiveLogix' },
        },
        {
          path: 'dispatch',
          component: () => import('@/views/DispatchCenter/index.vue'),
          meta: { title: '实时指挥中心 · HiveLogix' },
        },
        {
          path: 'orders',
          component: () => import('@/views/OrderTask/index.vue'),
          meta: { title: '订单与任务管理 · HiveLogix' },
        },
        {
          path: 'fleet',
          component: () => import('@/views/FleetManagement/index.vue'),
          meta: { title: '载具管理调度 · HiveLogix' },
        },
        {
          path: 'infra',
          component: () => import('@/views/Infrastructure/index.vue'),
          meta: { title: '基础设施配置 · HiveLogix' },
        },
        {
          path: 'simulation',
          component: () => import('@/views/SimulationBox/index.vue'),
          meta: { title: '仿真与配置 · HiveLogix' },
        },
      ],
    },

    // 404 兜底（保留旧占位页供复用）
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

// 动态更新页面标题
router.afterEach((to) => {
  document.title = (to.meta.title as string) ?? 'HiveLogix'
})

export default router
