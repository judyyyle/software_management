/**
 * geoLayout — 坐标布局纯函数
 *
 * 约束：
 *   - 纯函数，不 import 任何 Store / Service / Vue 响应式对象
 *   - 不持有任何状态，可直接用于单元测试
 *   - Bbox 结构与 stores/scene.ts SceneBounds 结构相同（结构兼容，无 import 依赖）
 */

/**
 * WGS84 经纬度边界框
 * 字段名与 stores/scene.ts SceneBounds 对齐，传入 SceneBounds 实例时 TypeScript
 * 结构类型检查自动兼容，无需额外转换。
 */
export interface Bbox {
  min_lng: number
  max_lng: number
  min_lat: number
  max_lat: number
}

export interface GeoLayoutOptions {
  /**
   * 随机扰动幅度，相对于格子宽 / 高的比例，取值 0~0.5
   * 默认 0.15（±15%），防止点落在格子边界上
   */
  jitter?: number
}

/**
 * 均匀栅格布局：将 n 个点均匀分散在 bbox 内，每点在格子中心附近随机扰动。
 *
 * 算法：
 *   rows = ceil(sqrt(n))
 *   cols = ceil(n / rows)
 *   将 bbox 等分为 rows × cols 格子
 *   每点 = 格子中心 + rand(±jitter × 格子宽/高)
 *   越界坐标截断到 bbox 边界内
 *
 * @param n       需要生成的点数量（≥1）
 * @param bbox    目标边界框（WGS84）
 * @param options 可选配置（jitter 扰动幅度）
 * @returns       长度恰好为 n 的坐标数组
 */
export function gridLayout(
  n: number,
  bbox: Bbox,
  options?: GeoLayoutOptions,
): Array<{ lng: number; lat: number }> {
  if (n <= 0) return []

  const jitter = options?.jitter ?? 0.15
  const rows   = Math.ceil(Math.sqrt(n))
  const cols   = Math.ceil(n / rows)
  const cellW  = (bbox.max_lng - bbox.min_lng) / cols
  const cellH  = (bbox.max_lat - bbox.min_lat) / rows

  const points: Array<{ lng: number; lat: number }> = []

  outer: for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (points.length >= n) break outer

      const centerLng = bbox.min_lng + (c + 0.5) * cellW
      const centerLat = bbox.min_lat + (r + 0.5) * cellH
      const dLng      = (Math.random() * 2 - 1) * jitter * cellW
      const dLat      = (Math.random() * 2 - 1) * jitter * cellH

      points.push({
        lng: Math.max(bbox.min_lng, Math.min(bbox.max_lng, centerLng + dLng)),
        lat: Math.max(bbox.min_lat, Math.min(bbox.max_lat, centerLat + dLat)),
      })
    }
  }

  return points
}
