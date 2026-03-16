/**
 * MQTT 模块类型定义
 * Requirements: 10.1, 10.2, 10.3, 10.4
 */

// ==================== 连接层类型 ====================

/**
 * MQTT 连接配置
 */
export interface MqttConnectionConfig {
    host: string
    port: number | string
    username?: string
    password?: string
    clientId?: string
    keepalive?: number
    reconnectPeriod?: number
    connectTimeout?: number
}

/**
 * 连接状态枚举
 */
export type ConnectionStatusType = 'connecting' | 'connected' | 'disconnected' | 'error'

/**
 * 连接错误类型
 */
export type ConnectionErrorType = 'timeout' | 'auth' | 'network' | 'unknown'

/**
 * 连接错误信息
 */
export interface ConnectionError {
    type: ConnectionErrorType
    message: string
    timestamp: number
    reconnectAttempts?: number
}

/**
 * 连接状态
 */
export interface ConnectionState {
    status: ConnectionStatusType
    lastConnectedAt?: number
    lastDisconnectedAt?: number
    reconnectAttempts: number
    error?: ConnectionError
}

/**
 * 重连策略配置
 */
export interface ReconnectStrategyConfig {
    initialDelay: number      // 初始重连延迟 (ms)，默认 3000
    maxDelay: number          // 最大重连延迟 (ms)，默认 60000
    maxAttempts: number       // 最大重连次数 (0 = 无限)
    backoffMultiplier: number // 退避倍数，默认 2
}

/**
 * 心跳管理器配置
 */
export interface HeartbeatConfig {
    interval: number    // 心跳间隔 (ms)，默认 30000
    timeout: number     // 超时时间 (ms)，默认 10000
}

// ==================== 服务层类型 ====================

/**
 * 主题订阅信息
 */
export interface TopicSubscription {
    topic: string
    qos: number
    subscribedAt: number
    source: string  // 订阅来源标识
}

/**
 * 消息处理器类型
 */
export type MessageHandler = (topic: string, payload: any) => void

/**
 * 消息处理器注册信息
 */
export interface MessageHandlerRegistration {
    id: string
    pattern: string
    handler: MessageHandler
}

/**
 * 待处理请求
 */
export interface PendingRequest {
    tid: string
    bid: string
    method: string
    sentAt: number
    timeout: number
    resolve: (response: any) => void
    reject: (error: Error) => void
    timeoutId?: ReturnType<typeof setTimeout>
}

// ==================== 消息类型 ====================

/**
 * MQTT 基础消息结构
 */
export interface MqttBaseMessage {
    tid: string       // 事务 ID
    bid: string       // 业务 ID
    timestamp: number
    method?: string
    data?: any
}

/**
 * MQTT 消息（内部使用）
 */
export interface MqttMessage {
    topic: string
    payload: string | Buffer
    qos?: number
    retain?: boolean
}

// ==================== 设备 OSD 类型 ====================

/**
 * 子设备信息（无人机）
 */
export interface SubDeviceInfo {
    device_sn: string
    device_online?: boolean
    device_paired?: boolean
    device_model_key?: string
}

/**
 * 设备 OSD 数据
 */
export interface DeviceOsd {
    mode_code?: number
    sub_device?: SubDeviceInfo
    drone_in_dock?: number
    cover_state?: number
    putter_state?: number
    supplement_light_state?: number
    network_state?: {
        type?: number
        quality?: number
        rate?: number
    }
    drone_charge_state?: {
        state?: number
        capacity_percent?: number
    }
    rainfall?: number
    wind_speed?: number
    environment_temperature?: number
    temperature?: number
    humidity?: number
    latitude?: number
    longitude?: number
    height?: number
    alternate_land_point?: {
        latitude?: number
        longitude?: number
        height?: number
    }
    first_power_on?: number
    positionState?: {
        gps_number?: number
        is_fixed?: number
        rtk_number?: number
    }
    storage?: {
        total?: number
        used?: number
    }
    electric_supply_voltage?: number
    working_voltage?: number
    working_current?: number
    backup_battery?: {
        voltage?: number
        temperature?: number
        switch?: number
    }
    drone_battery_maintenance_info?: {
        maintenance_state?: number
        maintenance_time_left?: number
    }
}

// ==================== 无人机 OSD 类型 ====================

/**
 * 相机信息
 */
export interface CameraInfo {
    payload_index?: string
    camera_mode?: number
    photo_state?: number
    recording_state?: number
    remain_photo_num?: number
    remain_record_duration?: number
    zoom_factor?: number
    ir_zoom_factor?: number
}

/**
 * 电池信息
 */
export interface BatteryInfo {
    capacity_percent?: number
    voltage?: number
    temperature?: number
    remain_flight_time?: number
    return_home_power?: number
    landing_power?: number
}

/**
 * 无人机 OSD 数据
 */
export interface DroneOsd {
    latitude?: number
    longitude?: number
    height?: number           // 相对海平面高度 (ASL)
    elevation?: number        // 相对起飞点高度 (ALT)
    horizontal_speed?: number
    vertical_speed?: number
    attitude_head?: number    // 偏航角
    attitude_pitch?: number
    attitude_roll?: number
    home_distance?: number
    wind_speed?: number
    wind_direction?: number
    battery?: BatteryInfo
    cameras?: CameraInfo[]
    gimbal_pitch?: number
    gimbal_roll?: number
    gimbal_yaw?: number
    mode_code?: number
    gear?: number
    position_state?: {
        gps_number?: number
        is_fixed?: number
        rtk_number?: number
    }
    obstacle_avoidance?: {
        horizon?: number
        upside?: number
        downside?: number
    }
    storage?: {
        total?: number
        used?: number
    }
    total_flight_time?: number
    total_flight_distance?: number
    maintain_status?: {
        maintain_status_array?: any[]
    }
}

// ==================== 服务请求/响应类型 ====================

/**
 * 服务请求消息
 */
export interface ServiceRequest extends MqttBaseMessage {
    method: string
    data: any
}

/**
 * 服务响应消息
 */
export interface ServiceResponse extends MqttBaseMessage {
    method: string
    data: {
        result: number
        output?: any
    }
}

/**
 * 事件消息
 */
export interface EventMessage extends MqttBaseMessage {
    method: string
    data: any
}

// ==================== DRC 类型 ====================

/**
 * DRC 控制消息
 */
export interface DrcControlMessage {
    seq: number
    method: string
    data: any
}

/**
 * DRC OSD 数据
 */
export interface DrcOsdData {
    latitude?: number
    longitude?: number
    height?: number
    attitude_head?: number
    speed_x?: number
    speed_y?: number
    speed_z?: number
    gimbal_pitch?: number
    gimbal_roll?: number
    gimbal_yaw?: number
}

/**
 * DRC 心跳消息
 */
export interface DrcHeartbeat {
    method: 'heart_beat'
    data: {
        timestamp: number
    }
    seq: number
}

// ==================== 主题模式 ====================

/**
 * 主题模式工具
 */
export const TopicPatterns = {
    // 设备 OSD
    DEVICE_OSD: (sn: string) => `thing/product/${sn}/osd`,
    DEVICE_STATE: (sn: string) => `thing/product/${sn}/state`,

    // 服务
    SERVICES: (sn: string) => `thing/product/${sn}/services`,
    SERVICES_REPLY: (sn: string) => `thing/product/${sn}/services_reply`,

    // 事件
    EVENTS: (sn: string) => `thing/product/${sn}/events`,
    EVENTS_REPLY: (sn: string) => `thing/product/${sn}/events_reply`,

    // 属性设置
    PROPERTY_SET: (sn: string) => `thing/product/${sn}/property/set`,

    // DRC 指令飞行
    DRC_UP: (sn: string) => `thing/product/${sn}/drc/up`,
    DRC_DOWN: (sn: string) => `thing/product/${sn}/drc/down`,

    // 系统状态
    SYS_STATUS: (sn: string) => `sys/product/${sn}/status`,
} as const

// ==================== 错误码映射 ====================

/**
 * DJI 错误码映射表
 */
export const ErrorCodeMap: Record<number, string> = {
    0: '成功',
    1: '参数错误',
    2: '设备忙',
    3: '设备离线',
    4: '设备未就绪',
    5: '执行超时',
    6: '执行失败',
    7: '权限不足',
    // 可根据实际需要扩展
}

/**
 * 获取错误信息
 */
export function getErrorMessage(code: number): string {
    return ErrorCodeMap[code] || `未知错误 (${code})`
}

// ==================== 默认配置 ====================

/**
 * 默认连接配置
 */
export const DEFAULT_CONNECTION_CONFIG: MqttConnectionConfig = {
    host: '',
    port: 8083,
    keepalive: 60,
    reconnectPeriod: 1000,
    connectTimeout: 30000,
}

/**
 * 默认重连策略配置
 */
export const DEFAULT_RECONNECT_CONFIG: ReconnectStrategyConfig = {
    initialDelay: 3000,
    maxDelay: 60000,
    maxAttempts: 0, // 无限重试
    backoffMultiplier: 2,
}

/**
 * 默认心跳配置
 * interval: 心跳检测间隔（毫秒）
 * timeout: 心跳超时时间（毫秒）- 如果在此时间内没有收到任何消息，则认为连接断开
 */
export const DEFAULT_HEARTBEAT_CONFIG: HeartbeatConfig = {
    interval: 30000,
    timeout: 60000, // 60秒没有消息才认为连接断开
}
