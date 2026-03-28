/**
 * sceneStore — 仿真场景上下文状态
 *
 * 职责：
 *   作为 SimulationBox/GeoTool 与 DispatchCenter 之间的唯一数据桥梁。
 *   两端视图不直接 import 对方，通过本 store 传递 SceneContext。
 *
 * 数据流：
 *   GeoTool 完成分析后 → prepareScene() → sceneStore.context 写入
 *   router.push('/dispatch') → DispatchCenter 读取 sceneStore.context
 *   DispatchCenter UnifiedMapView 挂载时并行请求建筑/路网数据
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

// ── 类型定义（与后端 SceneContext 协议对齐）─────────────────────────────────

/** UTM 原点偏移量，用于 physics engine 还原 SUMO 坐标 */
export interface NetOffset {
  /** UTM 东向原点（米），对应选区左下角经度 */
  ox: number
  /** UTM 北向原点（米），对应选区左下角纬度 */
  oy: number
}

/** 场景元数据（轻量级，随 /api/scene/prepare 响应返回） */
export interface SceneMeta {
  road_source:  'osm' | 'grid'
  road_nodes:   number
  road_edges:   number
  created_at:   string
  utm_zone:     number
  utm_band:     string
  net_offset:   NetOffset
}

/** 路网节点（WGS84 原始经纬度） */
export interface RoadNode {
  id:  string
  lng: number
  lat: number
}

/** 路网边（折线 shape，WGS84 经纬度序列） */
export interface RoadEdge {
  shape: [number, number][]   // [lng, lat] 序列
}

/** 路网地理范围 */
export interface SceneBounds {
  min_lng: number
  min_lat: number
  max_lng: number
  max_lat: number
}

/** 完整场景上下文（不含 buildings GeoJSON，建筑数据由 UnifiedMapView 独立加载） */
export interface SceneContext {
  scene_id:     string
  sel_bounds:   { minx: number; miny: number; maxx: number; maxy: number }
  threshold:    number
  height_column: string | null
  meta:         SceneMeta
  road_network: {
    nodes:  RoadNode[]
    edges:  RoadEdge[]
    bounds: SceneBounds
  }
}

// ── Store 定义 ───────────────────────────────────────────────────────────────

export const useSceneStore = defineStore('scene', () => {
  /** 当前已加载的场景上下文，null 表示尚未导出 */
  const context = ref<SceneContext | null>(null)

  /** /api/scene/prepare 请求进行中 */
  const loading = ref(false)

  /** 最近一次错误信息，成功后清空 */
  const error   = ref<string | null>(null)

  /**
   * 调用后端 /api/scene/prepare，打包场景上下文到 store。
   * 支持幂等：相同参数请求后端直接返回缓存结果。
   * loading 为 true 时阻止重复触发。
   */
  async function prepareScene(payload: {
    sel_bounds:     { minx: number; miny: number; maxx: number; maxy: number }
    threshold:      number
    height_column:  string | null
  }): Promise<void> {
    if (loading.value) return
    loading.value = true
    error.value   = null

    try {
      const res = await fetch('/api/scene/prepare', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...payload.sel_bounds,
          threshold:     payload.threshold,
          height_column: payload.height_column,
        }),
      })

      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error((body as { error?: string }).error ?? `HTTP ${res.status}`)
      }

      context.value = (await res.json()) as SceneContext
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** 清除场景（用于重新选区或退出仿真） */
  function clear(): void {
    context.value = null
    error.value   = null
  }

  return { context, loading, error, prepareScene, clear }
})
