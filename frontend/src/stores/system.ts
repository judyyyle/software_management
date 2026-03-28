import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 全局仿真系统状态 */
export const useSystemStore = defineStore('system', () => {
  const running    = ref(false)
  const simTime    = ref(0)       // 仿真当前时刻（秒）
  const speedRatio = ref(1)       // 仿真加速倍率

  function start()  { running.value = true  }
  function pause()  { running.value = false }
  function reset()  { running.value = false; simTime.value = 0 }

  return { running, simTime, speedRatio, start, pause, reset }
})
