/**
 * 直播服务 V2 - 基于 LiveSessionManager 的兼容层
 *
 * 提供与旧版 liveService 相似的 API，但底层使用 LiveSessionManager 支持多路直播
 *
 * 使用方式：
 * ```typescript
 * import { createLiveService } from '/@/services/liveServiceV2'
 *
 * // 为特定设备创建直播服务实例
 * const liveService = createLiveService({
 *   gatewaySn: '7CTXN3V00B08N2',
 *   deviceSn: '1581F6QAD247R00GG53P',
 *   cabinCameraIndex: '165-0-7',
 *   droneCameraIndex: '80-0-0'
 * })
 *
 * // 开始机舱直播
 * await liveService.startLive('cabin')
 *
 * // 开始无人机直播
 * await liveService.startLive('drone')
 * ```
 */

import { ref } from 'vue'
import { liveSessionManager, LiveSessionConfig, LiveSession } from './LiveSessionManager'
import { LiveStatus } from '/@/config/live'

export interface LiveServiceOptions {
    /** 机场 SN */
    gatewaySn: string
    /** 无人机 SN */
    deviceSn?: string
    /** 机舱相机索引 */
    cabinCameraIndex?: string
    /** 机舱视频索引 */
    cabinVideoIndex?: string
    /** 无人机相机索引 */
    droneCameraIndex?: string
    /** 无人机视频索引 */
    droneVideoIndex?: string
}

// 生成唯一的服务实例 ID
let serviceInstanceCounter = 0

/**
 * 创建直播服务实例（支持多路直播）
 */
export function createLiveService(options: LiveServiceOptions) {
    const {
        gatewaySn,
        deviceSn,
        cabinCameraIndex = '165-0-7',
        cabinVideoIndex = 'normal-0',
        droneCameraIndex = '80-0-0',
        droneVideoIndex = 'normal-0',
    } = options

    // 服务实例唯一 ID（用于事件监听器隔离）
    const serviceInstanceId = ++serviceInstanceCounter

    // 会话 ID
    const cabinSessionId = `cabin_${gatewaySn}`
    const droneSessionId = `drone_${deviceSn || gatewaySn}`

    // 状态
    const liveStatus = ref<LiveStatus>(LiveStatus.IDLE)
    const connectionQuality = ref<string>('未知')

    // 事件回调（本服务实例的回调）
    const eventCallbacks: Map<string, Function[]> = new Map()

    // 已绑定的 LiveSessionManager 事件处理器（用于清理）
    const boundHandlers: { event: string; handler: Function }[] = []

    // 是否已绑定事件
    let eventsBound = false

    // 绑定 LiveSessionManager 事件（带隔离）
    const bindEvents = (sessionId: string) => {
        // 如果已绑定，先解绑
        if (eventsBound) {
            unbindEvents()
        }

        const createHandler = (eventName: string, filter: (data: any) => boolean, transform?: (data: any) => any) => {
            const handler = (data: any) => {
                if (filter(data)) {
                    emit(eventName, transform ? transform(data) : data)
                }
            }
            boundHandlers.push({ event: eventName.replace('agora:', '').replace('live:', ''), handler })
            return handler
        }

        // 视频流事件
        const videoTrackHandler = (data: any) => {
            if (data.sessionId === sessionId) {
                emit('agora:videoTrack', data)
            }
        }
        liveSessionManager.on('agora:videoTrack', videoTrackHandler)
        boundHandlers.push({ event: 'agora:videoTrack', handler: videoTrackHandler })

        // 连接状态事件
        const connectionStateHandler = (data: any) => {
            if (data.sessionId === sessionId) {
                emit('agora:connectionState', data)
            }
        }
        liveSessionManager.on('agora:connectionState', connectionStateHandler)
        boundHandlers.push({ event: 'agora:connectionState', handler: connectionStateHandler })

        // 直播状态事件
        const liveStatusHandler = (data: any) => {
            if (data.sessionId === sessionId) {
                liveStatus.value = data.status
                emit('live:status', data)
            }
        }
        liveSessionManager.on('live:status', liveStatusHandler)
        boundHandlers.push({ event: 'live:status', handler: liveStatusHandler })

        // 直播错误事件
        const liveErrorHandler = (data: any) => {
            if (data.sessionId === sessionId) {
                emit('live:error', data)
            }
        }
        liveSessionManager.on('live:error', liveErrorHandler)
        boundHandlers.push({ event: 'live:error', handler: liveErrorHandler })

        // 声网错误事件
        const agoraErrorHandler = (data: any) => {
            if (data.sessionId === sessionId) {
                emit('agora:error', data)
            }
        }
        liveSessionManager.on('agora:error', agoraErrorHandler)
        boundHandlers.push({ event: 'agora:error', handler: agoraErrorHandler })

        // 离开频道事件
        const leftHandler = (data: any) => {
            if (data.sessionId === sessionId) {
                emit('agora:left', data)
            }
        }
        liveSessionManager.on('agora:left', leftHandler)
        boundHandlers.push({ event: 'agora:left', handler: leftHandler })

        eventsBound = true
        console.log(`[LiveServiceV2#${serviceInstanceId}] 事件已绑定: sessionId=${sessionId}`)
    }

    // 解绑事件
    const unbindEvents = () => {
        for (const { event, handler } of boundHandlers) {
            liveSessionManager.off(event, handler)
        }
        boundHandlers.length = 0
        eventsBound = false
        console.log(`[LiveServiceV2#${serviceInstanceId}] 事件已解绑`)
    }

    // 事件系统
    const on = (event: string, callback: Function) => {
        if (!eventCallbacks.has(event)) {
            eventCallbacks.set(event, [])
        }
        eventCallbacks.get(event)!.push(callback)
    }

    const off = (event: string, callback?: Function) => {
        if (!callback) {
            eventCallbacks.delete(event)
        } else {
            const callbacks = eventCallbacks.get(event)
            if (callbacks) {
                const index = callbacks.indexOf(callback)
                if (index > -1) callbacks.splice(index, 1)
            }
        }
    }

    const emit = (event: string, data: any) => {
        const callbacks = eventCallbacks.get(event)
        if (callbacks) {
            callbacks.forEach((callback) => callback(data))
        }
    }

    /**
     * 开始直播
     */
    const startLive = async (type: 'cabin' | 'drone') => {
        const sessionId = type === 'cabin' ? cabinSessionId : droneSessionId
        const config: LiveSessionConfig = type === 'cabin'
            ? {
                sessionId: cabinSessionId,
                type: 'cabin',
                gatewaySn,
                cameraIndex: cabinCameraIndex,
                videoIndex: cabinVideoIndex,
            }
            : {
                sessionId: droneSessionId,
                type: 'drone',
                gatewaySn,
                deviceSn,
                cameraIndex: droneCameraIndex,
                videoIndex: droneVideoIndex,
            }

        // 绑定事件（会自动清理旧的监听器）
        bindEvents(sessionId)

        // 创建会话（如果不存在）
        await liveSessionManager.createSession(config)

        // 开始直播
        await liveSessionManager.startLive(sessionId)
    }

    /**
     * 停止直播
     */
    const stopLive = async (type: 'cabin' | 'drone') => {
        const sessionId = type === 'cabin' ? cabinSessionId : droneSessionId
        await liveSessionManager.stopLive(sessionId)
    }

    /**
     * 获取会话
     */
    const getSession = (type: 'cabin' | 'drone'): LiveSession | undefined => {
        const sessionId = type === 'cabin' ? cabinSessionId : droneSessionId
        return liveSessionManager.getSession(sessionId)
    }

    /**
     * 获取状态
     */
    const getStatus = () => {
        return {
            liveStatus: liveStatus.value,
            connectionQuality: connectionQuality.value,
            cabinSession: liveSessionManager.getSession(cabinSessionId),
            droneSession: liveSessionManager.getSession(droneSessionId),
        }
    }

    /**
     * 销毁
     */
    const destroy = async () => {
        // 先解绑事件
        unbindEvents()
        // 销毁会话
        await liveSessionManager.destroySession(cabinSessionId)
        await liveSessionManager.destroySession(droneSessionId)
        eventCallbacks.clear()
        console.log(`[LiveServiceV2#${serviceInstanceId}] 服务已销毁`)
    }

    /**
     * 设置重试配置（兼容旧 API）
     */
    const setRetryConfig = (maxRetries: number, retryDelay: number) => {
        console.log(`[LiveServiceV2#${serviceInstanceId}] 重试配置: maxRetries=${maxRetries}, retryDelay=${retryDelay}`)
        // LiveSessionManager 暂不支持重试配置，这里只是兼容旧 API
    }

    return {
        startLive,
        stopLive,
        getSession,
        getStatus,
        destroy,
        setRetryConfig,
        on,
        off,
        // 暴露会话 ID，方便调试
        cabinSessionId,
        droneSessionId,
        // 暴露服务实例 ID，方便调试
        serviceInstanceId,
    }
}

// 导出类型
export type LiveServiceV2 = ReturnType<typeof createLiveService>
