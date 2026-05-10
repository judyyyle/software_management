import { defineStore } from 'pinia'
import { ref } from 'vue'
import { http } from '@/services/http'
import { WsClient } from '@/services/websocket'
import { useEntityStore } from './entity'
import { useOrderStore } from './order'
import { useSceneStore } from './scene'

// ── 调试开关 ────────────────────────────────────────────────────
const DEBUG_WEBSOCKET = false

/** 全局仿真系统状态 */
export const useSystemStore = defineStore('system', () => {
  const running          = ref(false)
  const simTime          = ref(0)       // 仿真当前时刻（秒）
  const speedRatio       = ref(1)       // 仿真加速倍率
  const simStartWallMs   = ref(0)       // 引擎启动时的 wall-clock 毫秒时间戳
  const initialized      = ref(false)   // 是否已完成初始化

  // ── WebSocket 客户端（19 为展示层与店间解耦层────────────────
  // 这里使用延迟初始化模式：实际 ws 实例在 initWs() 中创建，避免 Store 定义时 Pinia 尚未就绪
  let _ws: WsClient | null = null
  let _tickCallbacks: Array<() => void> = []

  // ── TICK 回调管理 ──────────────────────────────────────────────
  function onTick(callback: () => void) {
    _tickCallbacks.push(callback)
  }

  // ── FULL_SNAPSHOT 处理器 ───────────────────────────────────────
  function handleFullSnapshot(payload: any) {
    if (DEBUG_WEBSOCKET) {
      console.log('[SystemStore.handleFullSnapshot] 收到快照，实体数量:', {
        depots: payload.entities?.depots?.length ?? 0,
        stations: payload.entities?.stations?.length ?? 0,
        trucks: payload.entities?.trucks?.length ?? 0,
        drones: payload.entities?.drones?.length ?? 0,
      })
    }
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
    const trucks_count = payload.entities?.trucks?.length ?? 0
    if (DEBUG_WEBSOCKET && trucks_count > 0) {
      const truck = payload.entities.trucks[0]
      console.log(`[SystemStore.handleTick] 第一辆卡车详情:`, {
        truck_id: truck.truck_id,
        lng: truck.lng,
        lat: truck.lat,
        status: truck.status,
      })
    }
    entityStore.setRuntimeAll(payload.entities ?? {})
    
    // ── 处理订单状态更新 ─────────────────────────────────────
    const orderStore = useOrderStore()
    if (payload.orders && Array.isArray(payload.orders)) {
      // 根据 backend 推送的订单状态更新 frontend 订单
      const completedCount = payload.orders.filter((o: any) => o.status === 'COMPLETED').length
      if (DEBUG_WEBSOCKET && (completedCount > 0 || payload.orders.length > 0)) {
        console.log(`[SystemStore.handleTick] 收到 ${payload.orders.length} 条订单，其中已完成: ${completedCount}`)
        
        // 显示前几条订单的详情
        if (payload.orders.length > 0) {
          console.log(`  首条订单: id=${payload.orders[0].order_id}, status=${payload.orders[0].status}`)
        }
      }
      
      for (const backendOrder of payload.orders) {
        const normalizedOrder = {
          order_id: backendOrder.order_id,
          status: backendOrder.status,
          source_type: backendOrder.source_type ?? null,
          pickup_source_id: backendOrder.pickup_source_id ?? null,
          payload_weight: Number(backendOrder.payload_weight ?? 0),
          create_time: Number(backendOrder.create_time ?? 0),
          deadline: Number(backendOrder.deadline ?? 0),
          actual_deliver_time: backendOrder.actual_deliver_time ?? undefined,
          assigned_vehicle_id: backendOrder.assigned_vehicle_id ?? undefined,
          assigned_mode: backendOrder.assigned_mode ?? undefined,
          delivery_lng: Number(backendOrder.delivery_lng ?? 0),
          delivery_lat: Number(backendOrder.delivery_lat ?? 0),
          delivery_z: Number(backendOrder.delivery_z ?? 0),
          time_domain: backendOrder.time_domain ?? 'sim_s',
        }

        const idx = orderStore.generatedOrders.findIndex(o => o.order_id === normalizedOrder.order_id)
        if (idx >= 0) {
          const oldStatus = orderStore.generatedOrders[idx].status
          Object.assign(orderStore.generatedOrders[idx], normalizedOrder)
          if (DEBUG_WEBSOCKET && normalizedOrder.status !== oldStatus) {
            console.log(`  订单状态: ${oldStatus} → ${normalizedOrder.status}`)
          }
        } else {
          orderStore.generatedOrders.push(normalizedOrder as any)
          if (DEBUG_WEBSOCKET && completedCount > 0) {
            console.warn(`[SystemStore.handleTick] 新增后端订单 ${normalizedOrder.order_id} 到前端列表`)
          }
        }
      }
    }
    
    if (payload.stats !== undefined) {
      orderStore.stats = payload.stats
    }
    // 触发所有已注册的 TICK 回调
    _tickCallbacks.forEach(cb => {
      try {
        cb()
      } catch (e) {
        console.error('[onTick callback error]', e)
      }
    })
  }

  // ── WebSocket 初始化（页面挂载时调用）────────────────────────
  function initWs() {
    if (_ws) return   // 防止重复初始化
    // 使用相对路径，经 Vite 代理转发，生产环境同样适用
    const wsBase = import.meta.env.VITE_WS_BASE ?? `ws://${location.host}`
    const wsUrl = `${wsBase}/api/ws/telemetry`
    if (DEBUG_WEBSOCKET) {
      console.log(`[SystemStore] 初始化 WebSocket: ${wsUrl}`)
    }
    _ws = new WsClient(wsUrl)
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
    initialized.value = false
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
   * 同时将前端已生成的静态订单一并发送，成为初始订单池。
   * bbox 可选：已加载仿真场景时由调用方传入，否则使用默认值。
   */
  async function initSim(opts: {
    bbox?: { min_lng: number; min_lat: number; max_lng: number; max_lat: number }
    sceneId?: string
  } = {}) {
    const entityStore = useEntityStore()
    const orderStore  = useOrderStore()
    const bbox = opts.bbox ?? { min_lng: 121.40, min_lat: 31.10, max_lng: 121.60, max_lat: 31.30 }
    
    // 将前端生成的订单转换为后端格式（包含仓库信息）
    const initialOrders = orderStore.generatedOrders.map(o => ({
      order_id: o.order_id,
      create_time: o.create_time,
      deadline: o.deadline,
      delivery_lng: o.delivery_lng,
      delivery_lat: o.delivery_lat,
      delivery_z: o.delivery_z,
      payload_weight: o.payload_weight,
      status: o.status,
      source_type: o.source_type ?? null,
      pickup_source_id: o.pickup_source_id ?? null,
      time_domain: o.time_domain,
    }))
    
    // 👇 新增：记录要发送的充电站坐标，用于对比
    const stationsToSend = entityStore.stations.map(s => ({
      id: s.station_id,
      lng: s.lng,
      lat: s.lat
    }))
    console.log('[initSim] 发送充电站坐标到后端:', stationsToSend)
    
    const result = await http.post<any>('/api/sim/init', {
      scene_id: opts.sceneId ?? 'ui-test',
      bbox,
      entities: {
        depots:   entityStore.depots,
        stations: entityStore.stations,
        trucks:   entityStore.trucks,
        drones:   entityStore.drones,
      },
      order_gen_config: orderStore.generatorConfig,
      initial_orders: initialOrders,  // 发送前端已生成的静态订单
      scheduled_dynamic_orders: orderStore.scheduledDynamicOrders,
    })
    // 初始化成功后，标记为已初始化
    initialized.value = true
    return result
  }

  /**
   * 触发调度决策。
   * 将待分配订单传送到后端，并可指定求解算法。
   */
  async function dispatch(
    bbox: { minx: number; miny: number; maxx: number; maxy: number },
    solver: 'greedy' | 'greedy_mmce' | 'greedy_mmce_bi' | 'ga_mmce' | 'market' = 'greedy',
    options: { reuseStaticGaPlan?: boolean } = {},
  ) {
    if (DEBUG_WEBSOCKET) {
      console.log('[system.dispatch] 被调用，bbox:', bbox)
    }
    
    // 获取当前场景的 scene_id（用于后端加载缓存的 OSM 数据）
    const sceneStore = useSceneStore()
    const sceneId = sceneStore.context?.scene_id
    
    const payload: any = { solver, bbox }
    if (sceneId) {
      payload.scene_id = sceneId
    }
    if (solver === 'ga_mmce' && options.reuseStaticGaPlan) {
      payload.reuse_static_ga_plan = true
    }
    
    if (DEBUG_WEBSOCKET) {
      console.log('[system.dispatch] 发送的 payload:', payload)
    }
    const result = await http.post<any>('/api/sim/dispatch', payload)
    if (DEBUG_WEBSOCKET) {
      console.log('[system.dispatch] 后端响应:', result)
    }
    return result
  }

  return {
    running, simTime, speedRatio, simStartWallMs, initialized,
    initWs, probeBackend, onTick,
    start, pause, reset, setSpeed, initSim, dispatch,
  }
})
