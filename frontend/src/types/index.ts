/** HiveLogix 核心类型定义（与后端模型对齐） */

export interface LatLon {
  lat: number
  lon: number
}

// ── 实体 ──────────────────────────────────────────────────────────

export interface Depot {
  id: string
  location: LatLon
  name?: string
}

export interface Truck {
  id: string
  location: LatLon
  capacity: number          // C_truck（件）
  droneSlots: number        // 搭载无人机数量上限 K
  currentLoad: number
  drones: string[]          // 机载无人机 id 列表
  speed: number             // m/s
}

export interface Station {
  id: string
  location: LatLon
  swapTime: number          // τ_swap（秒）
  chargingPower?: number    // kW（充电模式）
}

export interface Drone {
  id: string
  location: LatLon
  maxPayload: number        // C_drone（kg）
  batteryMax: number        // E_max（Wh）
  batteryLevel: number      // 当前电量（Wh）
  speed: number             // m/s
  assignedTruck?: string    // 当前挂载卡车 id
}

// ── 订单 ──────────────────────────────────────────────────────────

export type FulfillmentMode = 'A' | 'B' | 'C' | 'D' | 'E'

export interface Order {
  id: string
  location: LatLon
  weight: number            // kg
  releaseTime: number       // 仿真时刻（秒）
  deadline: number          // 软时间窗截止时刻（秒）
  assignedMode?: FulfillmentMode
  status: 'pending' | 'in_transit' | 'delivered' | 'late'
}

// ── WebSocket 推送消息 ──────────────────────────────────────────────

export interface WsMessage<T = unknown> {
  type: string
  payload: T
}
