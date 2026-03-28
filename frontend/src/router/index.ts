import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/geo',
    },
    {
      path: '/geo',
      component: () => import('@/views/GeoTool/index.vue'),
      meta: { title: 'UAV 禁飞区地图' },
    },
    {
      path: '/monitor',
      component: () => import('@/views/MainMonitor/index.vue'),
      meta: { title: 'HiveLogix 调度大屏' },
    },
    {
      path: '/analytics',
      component: () => import('@/views/Analytics/index.vue'),
      meta: { title: '历史分析' },
    },
  ],
})

// 动态更新页面标题
router.afterEach((to) => {
  document.title = (to.meta.title as string) || 'HiveLogix'
})

export default router
