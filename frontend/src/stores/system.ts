import { defineStore } from 'pinia'
import { ref } from 'vue'
import { http } from '@/services/http'
import { WsClient } from '@/services/websocket'
import { useEntityStore } from './entity'
import { useOrderStore } from './order'

/** 全局仿真系统状态 */
export const useSystemStore = defineStore('system', () => {
  const running          = ref(false)
  const simTime          = ref(0)       // 仿真当前时刻（秒）
  const speedRatio       = ref(1)       // 仿真加速倍率
  const simStartWallMs   = ref(0)       // 引擎启动时的 wall-clock 毫秒时间戳

  // ── WebSocket 客户端（19 为展示层与店间解耦层────────────────
  // 这里使用延迟初始化模式：实际 ws 实例在 initWs() 中创建，避免 Store 定义时 Pinia 尚未就绪
  let _ws: WsClient | null = null

  // ── FULL_SNAPSHOT 处理器 ───────────────────────────────────────
  function handleFullSnapshot(payload: any) {
    const entityStore = useEntityStore()
    entityStore.setRuntimeAll({
      depots:   payload.entities?.depots,
      stations: payload.entities?.stations,
      trucks:   payload.entities?.trucks,
      drones:   payload.entities?.drones,
    })
    simStartWallMs.value = payload.sim_start_wall_ms ?? 0
    simTime.value        = payload.sim_time ?? 0
    running.value        = payload.is_running ?? false
    speedRatio.value     = payload.speed_ratio ?? 1
  }

  // ── TICK 处理器 ─────────────────────────────────────────────
  function handleTick(payload: any) {
    simTime.value = payload.sim_time ?? simTime.value
    const entityStore = useEntityStore()
    entityStore.setRuntimeAll(payload.entities ?? {})
    // v4.2 修正：赋值前需确认 orderStore 中 stats 已声明
    const orderStore = useOrderStore()
    if (payload.stats !== undefined) {
      orderStore.stats = payload.stats
    }
  }

  // ── WebSocket 初始化（页面挂载时调用）────────────────────────
  function initWs() {
    if (_ws) return   // 防止重复初始化
    // 使用相对路径，经 Vite 代理转发，生产环境同样适用
    const wsBase = import.meta.env.VITE_WS_BASE ?? `ws://${location.host}`
    _ws = new WsClient(`${wsBase}/api/ws/telemetry`)
    // 处理器必须在 connect() 之前注册（WsClient 要求）
    _ws.on('FULL_SNAPSHOT', handleFullSnapshot)
    _ws.on('TICK', handleTick)
    _ws.connect()
  }

  // ── 应用挂载探针：检查后端是否已在运行 ──────────────────
  async function probeBackend() {
    try {
      const state = await http.get<any>('/api/sim/state')
      if (state?.is_running) {
        // 后端已在运行：直接应用快照，跳过 /api/sim/init
        handleFullSnapshot(state)
      }
    } catch {
      // 后端未启动或网络不可达，保持本地配置态
    }
  }

  // ── 仿真控制 API ────────────────────────────────────────
  async function start() {
    await http.post('/api/sim/control', { action: 'start' })
    running.value = true
  }

  async function pause() {
    await http.post('/api/sim/control', { action: 'pause' })
    running.value = false
  }

  async function reset() {
    await http.post('/api/sim/control', { action: 'reset' })
    running.value = false
    simTime.value = 0
  }

  /** 调整仿真速率（并实时通知后端）*/
  async function setSpeed(ratio: number) {
    speedRatio.value = ratio
    // 使用专用 set_speed action，避免误重启已暂停的仿真
    await http.post('/api/sim/control', { action: 'set_speed', speed: ratio }).catch(() => {
      // 后端未启动时仅本地保存，不报错
    })
  }

  /**
   * 将当前 entityStore 配置 + orderStore 生成参数发送到后端 /api/sim/init。
   * bbox 可选：已加载仿真场景时由调用方传入，否则使用海量默认值。
   */
  async function initSim(opts: {
    bbox?: { min_lng: number; min_lat: number; max_lng: number; max_lat: number }
    sceneId?: string
  } = {}) {
    const entityStore = useEntityStore()
    const orderStore  = useOrderStore()
    const bbox = opts.bbox ?? { min_lng: 121.40, min_lat: 31.10, max_lng: 121.60, max_lat: 31.30 }
    return await http.post<any>('/api/sim/init', {
      scene_id: opts.sceneId ?? 'ui-test',
      bbox,
      entities: {
        depots:   entityStore.depots,
        stations: entityStore.stations,
        trucks:   entityStore.trucks,
        drones:   entityStore.drones,
      },
      order_gen_config: orderStore.generatorConfig,
    })
  }

  return {
    running, simTime, speedRatio, simStartWallMs,
    initWs, probeBackend,
    start, pause, reset, setSpeed, initSim,
  }
})
