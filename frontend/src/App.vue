<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useEntityStore } from '@/stores/entity'
import { useSystemStore } from '@/stores/system'
import { useSceneStore }  from '@/stores/scene'

const entityStore = useEntityStore()
const systemStore = useSystemStore()
const sceneStore  = useSceneStore()

onMounted(() => {
  entityStore.loadConfig()
  systemStore.initWs()       // 建立 WebSocket 遥测连接
  systemStore.probeBackend() // 探测后端是否已在运行（恢复快照）

  // 尝试从上次会话恢复场景上下文（页面刷新后生效）
  const cachedSceneId = localStorage.getItem('hl-scene-id')
  if (cachedSceneId) {
    sceneStore.restoreScene(cachedSceneId)
  }
})
</script>
