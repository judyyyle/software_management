import AgoraRTC, { IAgoraRTCClient } from 'agora-rtc-sdk-ng'
import { useMqttStore } from '/@/stores/mqtt'
import { agoraEvents, LiveStatus } from '/@/config/live'
import { DJIoperations } from '/@/utils/mqttSdk'
import createAxios from '/@/utils/axios'
import { getRtmpConfig, type RtmpConfigResponse } from '/@/api/backend/equipment/rtmp'

/** 直播模式：1=声网, 2=RTMP */
type LiveMode = 1 | 2

/**
 * 直播会话配置
 */
export interface LiveSessionConfig {
    /** 会话唯一标识（通常使用设备SN） */
    sessionId: string
    /** 直播类型：cabin=机舱, drone=无人机 */
    type: 'cabin' | 'drone'
    /** 机场SN（gateway_sn） */
    gatewaySn: string
    /** 设备SN（无人机SN，仅drone类型需要） */
    deviceSn?: string
    /** 相机索引 */
    cameraIndex: string
    /** 视频索引 */
    videoIndex: string
}

/**
 * 直播会话状态
 */
export interface LiveSession {
    config: LiveSessionConfig
    status: LiveStatus
    agoraClient: IAgoraRTCClient | null
    channel: string
    token: string
    uid: string
    videoId: string
    videoTrack: any
    connectionQuality: string
    error: string | null
    /** RTMP配置（RTMP模式下使用） */
    rtmpConfig?: RtmpConfigResponse
    /** RTMP播放地址（RTMP模式下使用） */
    rtmpPlayUrl?: string
}

/**
 * Token 缓存项
 */
interface TokenCacheItem {
    token: string
    expireAt: number
}

/**
 * 多路直播会话管理器
 *
 * 支持同时管理多个独立的直播会话，每个会话有独立的：
 * - 声网客户端实例
 * - 频道名（基于设备SN生成）
 * - Token（按需获取，支持缓存）
 *
 * 使用示例：
 * ```typescript
 * // 创建机舱直播会话
 * const session = await liveSessionManager.createSession({
 *   sessionId: 'cabin_7CTXN3V00B08N2',
 *   type: 'cabin',
 *   gatewaySn: '7CTXN3V00B08N2',
 *   cameraIndex: '165-0-7',
 *   videoIndex: 'normal-0'
 * })
 *
 * // 开始直播
 * await liveSessionManager.startLive('cabin_7CTXN3V00B08N2')
 *
 * // 停止直播
 * await liveSessionManager.stopLive('cabin_7CTXN3V00B08N2')
 * ```
 */
export class LiveSessionManager {
    private mqttStore = useMqttStore()

    /** 所有直播会话 */
    private sessions: Map<string, LiveSession> = new Map()

    /** Token 缓存（key: channel, value: token info） */
    private tokenCache: Map<string, TokenCacheItem> = new Map()

    /** Token 缓存有效期（毫秒），默认 50 分钟 */
    private tokenCacheTTL = 50 * 60 * 1000

    /** 声网 App ID */
    private appId = import.meta.env.VITE_AGORA_CABIN_APPID || ''

    /** 直播模式：1=声网, 2=RTMP */
    private liveMode: LiveMode = (parseInt(import.meta.env.VITE_VIDEO_TYPE || '1') as LiveMode) || 1

    /** RTMP配置缓存（key: gatewaySn） */
    private rtmpConfigCache: Map<string, RtmpConfigResponse> = new Map()

    /** 事件回调 */
    private eventCallbacks: Map<string, Function[]> = new Map()

    constructor() {
        console.log(`[LiveSessionManager] 初始化, 直播模式: ${this.liveMode === 1 ? '声网' : 'RTMP'}`)
    }

    /**
     * 获取当前直播模式
     */
    getLiveMode(): LiveMode {
        return this.liveMode
    }

    /**
     * 是否为RTMP模式
     */
    isRtmpMode(): boolean {
        return this.liveMode === 2
    }

    /**
     * 获取设备RTMP配置（带缓存）
     */
    private async getRtmpConfigForDevice(gatewaySn: string): Promise<RtmpConfigResponse | null> {
        // 检查缓存
        if (this.rtmpConfigCache.has(gatewaySn)) {
            return this.rtmpConfigCache.get(gatewaySn)!
        }

        try {
            const res = await getRtmpConfig(gatewaySn)
            if (res?.code === 1 && res?.data) {
                this.rtmpConfigCache.set(gatewaySn, res.data)
                return res.data
            }
        } catch (error) {
            console.error(`[LiveSessionManager] 获取RTMP配置失败:`, error)
        }
        return null
    }

    /**
     * 清除RTMP配置缓存
     */
    clearRtmpConfigCache(gatewaySn?: string): void {
        if (gatewaySn) {
            this.rtmpConfigCache.delete(gatewaySn)
        } else {
            this.rtmpConfigCache.clear()
        }
    }

    /**
     * 生成频道名（基于设备SN）
     */
    private generateChannelName(config: LiveSessionConfig): string {
        if (config.type === 'cabin') {
            return `cabin_${config.gatewaySn}`
        } else {
            return `drone_${config.deviceSn || config.gatewaySn}`
        }
    }

    /**
     * 生成 UID（基于时间戳，确保唯一性）
     */
    private generateUid(): string {
        return Date.now().toString().slice(-6)
    }

    /**
     * 生成 video_id
     */
    private generateVideoId(config: LiveSessionConfig): string {
        const sn = config.type === 'cabin' ? config.gatewaySn : config.deviceSn || config.gatewaySn
        return `${sn}/${config.cameraIndex}/${config.videoIndex}`
    }

    /**
     * 获取 Token（支持缓存）
     */
    private async getToken(channel: string, uid: string): Promise<string> {
        // 检查缓存
        const cached = this.tokenCache.get(channel)
        if (cached && cached.expireAt > Date.now()) {
            console.log(`[LiveSessionManager] 使用缓存的 Token: ${channel}`)
            return cached.token
        }

        // 请求新 Token
        console.log(`[LiveSessionManager] 请求新 Token: ${channel}`)
        try {
            const response = await createAxios(
                {
                    url: '/api/agora/token',
                    method: 'post',
                    data: {
                        channelName: channel,
                        uid: uid,
                        tokenExpireTs: 3600,
                        privilegeExpireTs: 3600,
                        serviceRtc: { enable: true, role: 1 },
                    },
                },
                { showCodeMessage: false }
            )

            if (response?.code === 1 && response?.data?.token) {
                const token = response.data.token
                // 缓存 Token
                this.tokenCache.set(channel, {
                    token,
                    expireAt: Date.now() + this.tokenCacheTTL,
                })
                console.log(`[LiveSessionManager] Token 获取成功: ${channel}`)
                return token
            } else {
                throw new Error(response?.msg || '获取 Token 失败')
            }
        } catch (error: any) {
            console.error(`[LiveSessionManager] 获取 Token 失败:`, error)
            throw error
        }
    }

    /**
     * 创建直播会话
     */
    async createSession(config: LiveSessionConfig): Promise<LiveSession> {
        const { sessionId } = config

        // 检查是否已存在
        if (this.sessions.has(sessionId)) {
            console.log(`[LiveSessionManager] 会话已存在: ${sessionId}`)
            return this.sessions.get(sessionId)!
        }

        const channel = this.generateChannelName(config)
        const uid = this.generateUid()
        const videoId = this.generateVideoId(config)

        // 创建声网客户端
        const agoraClient = AgoraRTC.createClient({ mode: 'live', codec: 'h264' })

        const session: LiveSession = {
            config,
            status: LiveStatus.IDLE,
            agoraClient,
            channel,
            token: '',
            uid,
            videoId,
            videoTrack: null,
            connectionQuality: '未知',
            error: null,
        }

        // 绑定声网事件
        this.bindAgoraEvents(session)

        this.sessions.set(sessionId, session)
        console.log(`[LiveSessionManager] 创建会话: ${sessionId}, channel: ${channel}`)

        this.emit('session:created', { sessionId, session })
        return session
    }

    /**
     * 绑定声网事件
     */
    private bindAgoraEvents(session: LiveSession) {
        const { agoraClient, config } = session
        if (!agoraClient) return

        agoraClient.on(agoraEvents.connectionStateChange, (curState: string, prevState: string) => {
            console.log(`[LiveSessionManager] ${config.sessionId} 连接状态: ${prevState} -> ${curState}`)
            if (curState === 'CONNECTED') {
                session.connectionQuality = '良好'
            } else if (curState === 'DISCONNECTED') {
                session.connectionQuality = '断开'
            }
            this.emit('agora:connectionState', { sessionId: config.sessionId, current: curState, previous: prevState })
        })

        agoraClient.on(agoraEvents.userPublished, async (user: any, mediaType: 'video' | 'audio') => {
            try {
                await agoraClient.subscribe(user, mediaType)
                if (mediaType === 'video') {
                    session.videoTrack = user.videoTrack
                    session.status = LiveStatus.LIVE
                    console.log(`[LiveSessionManager] ${config.sessionId} 视频流已订阅`)
                    this.emit('agora:videoTrack', { sessionId: config.sessionId, user, track: user.videoTrack })
                } else if (mediaType === 'audio') {
                    this.emit('agora:audioTrack', { sessionId: config.sessionId, user, track: user.audioTrack })
                }
            } catch (error) {
                console.error(`[LiveSessionManager] ${config.sessionId} 订阅失败:`, error)
                this.emit('agora:error', { sessionId: config.sessionId, error, operation: 'subscribe' })
            }
        })

        agoraClient.on(agoraEvents.userUnpublished, (user: any) => {
            session.videoTrack = null
            this.emit('agora:userUnpublished', { sessionId: config.sessionId, user })
        })

        agoraClient.on(agoraEvents.networkQuality, (stats: any) => {
            session.connectionQuality = this.getQualityText(stats.downlinkNetworkQuality)
            this.emit('agora:networkQuality', { sessionId: config.sessionId, stats })
        })

        agoraClient.on(agoraEvents.exception, (exception: any) => {
            console.error(`[LiveSessionManager] ${config.sessionId} 声网异常:`, exception)
            session.error = exception.msg || '声网异常'
            this.emit('agora:exception', { sessionId: config.sessionId, exception })
        })
    }

    /**
     * 开始直播
     */
    async startLive(sessionId: string): Promise<void> {
        const session = this.sessions.get(sessionId)
        if (!session) {
            throw new Error(`会话不存在: ${sessionId}`)
        }

        const { config, agoraClient, channel, uid, videoId } = session

        try {
            session.status = LiveStatus.STARTING
            session.error = null
            this.emit('live:status', { sessionId, status: LiveStatus.STARTING })

            // 检查 MQTT 连接
            if (!this.mqttStore.isConnected) {
                throw new Error('MQTT 未连接')
            }

            // 注意：不再预先停止旧直播，因为这可能导致推流中断
            // 如果需要重新开始，应该先调用 stopLive 再调用 startLive

            let liveData: any

            if (this.isRtmpMode()) {
                // ========== RTMP 直播模式 ==========
                console.log(`[LiveSessionManager] ${sessionId} 使用RTMP直播模式`)

                // 获取设备RTMP配置
                const rtmpConfig = await this.getRtmpConfigForDevice(config.gatewaySn)
                if (!rtmpConfig) {
                    throw new Error('未配置RTMP直播，请先在设备管理中配置推流密钥')
                }

                // 根据直播类型获取对应的推流地址
                const streamConfig = config.type === 'cabin' ? rtmpConfig.cabin : rtmpConfig.drone
                if (!streamConfig.push_url) {
                    throw new Error(`未配置${config.type === 'cabin' ? '机舱' : '飞行器'}推流地址`)
                }

                // 保存RTMP配置到会话
                session.rtmpConfig = rtmpConfig
                session.rtmpPlayUrl = streamConfig.play_url.flv

                // 构建RTMP直播数据
                liveData = {
                    url_type: 1, // 1=RTMP模式
                    url: streamConfig.push_url,
                    video_id: videoId,
                    video_quality: config.type === 'cabin' ? 4 : 0,
                }

                console.log(`[LiveSessionManager] ${sessionId} RTMP推流地址: ${streamConfig.push_url}`)
                console.log(`[LiveSessionManager] ${sessionId} RTMP播放地址: ${streamConfig.play_url.flv}`)

                // 发送 MQTT 命令开始推流
                await DJIoperations.sendServices(config.gatewaySn, 'live_start_push', liveData)
                DJIoperations.deviceServicesReply(config.gatewaySn)

                // RTMP模式下触发播放地址事件，供前端播放器使用
                this.emit('rtmp:playUrl', {
                    sessionId,
                    playUrl: streamConfig.play_url.flv,
                    hlsUrl: streamConfig.play_url.hls,
                    type: config.type,
                })
            } else {
                // ========== 声网直播模式 ==========
                console.log(`[LiveSessionManager] ${sessionId} 使用声网直播模式`)

                // 获取 Token（每次都重新获取，确保 Token 有效）
                session.token = await this.getToken(channel, uid)

                // 构建声网直播数据
                liveData = {
                    url_type: 0, // 0=声网模式
                    url: `channel=${channel}&sn=${config.gatewaySn}&token=${encodeURIComponent(session.token)}&uid=${uid}`,
                    video_id: videoId,
                    video_quality: config.type === 'cabin' ? 4 : 0,
                }
                // 发送 MQTT 命令开始推流
                await DJIoperations.sendServices(config.gatewaySn, 'live_start_push', liveData)
                DJIoperations.deviceServicesReply(config.gatewaySn)

                // 本地存储当前直播的数据
                localStorage.setItem(`liveConfig_${config.type}`, JSON.stringify({ ...config, videoId }))

                // 加入声网频道
                if (agoraClient) {
                    const connectionState = agoraClient.connectionState
                    if (connectionState !== 'CONNECTED' && connectionState !== 'CONNECTING') {
                        await agoraClient.setClientRole('audience')
                        await agoraClient.join(this.appId, channel, session.token, uid)
                        console.log(`[LiveSessionManager] ${sessionId} 已加入频道: ${channel}`)
                        this.emit('agora:joined', { sessionId, channel, uid })
                    }
                }
            }

            session.status = LiveStatus.LIVE
            this.emit('live:status', { sessionId, status: LiveStatus.LIVE })
            console.log(`[LiveSessionManager] ${sessionId} 直播已开始`)
        } catch (error: any) {
            console.error(`[LiveSessionManager] ${sessionId} 开始直播失败:`, error)
            session.status = LiveStatus.ERROR
            session.error = error.message || '开始直播失败'
            this.emit('live:error', { sessionId, error, operation: 'startLive' })
            throw error
        }
    }
    /**
     * 停止直播
     */
    async stopLive(sessionId: string): Promise<void> {
        const session = this.sessions.get(sessionId)
        if (!session) {
            console.warn(`[LiveSessionManager] 会话不存在: ${sessionId}`)
            return
        }

        const { config, agoraClient, channel, videoId } = session

        try {
            session.status = LiveStatus.STOPPING
            this.emit('live:status', { sessionId, status: LiveStatus.STOPPING })

            // 清除本地存储的直播数据,如果有则清除
            if (localStorage.getItem(`liveConfig_${config.type}`)) {
                localStorage.removeItem(`liveConfig_${config.type}`)
            }

            // 离开声网频道
            if (agoraClient && agoraClient.connectionState === 'CONNECTED') {
                await agoraClient.leave()
                console.log(`[LiveSessionManager] ${sessionId} 已离开频道: ${channel}`)
                this.emit('agora:left', { sessionId, channel })
            }

            console.log(`[LiveSessionManager] ${sessionId} 已离开频道: ${channel}`, videoId)

            // 发送 MQTT 命令停止推流
            await DJIoperations.sendServices(config.gatewaySn, 'live_stop_push', { video_id: videoId })
            DJIoperations.deviceServicesReplyClose(config.gatewaySn)

            session.status = LiveStatus.IDLE
            session.videoTrack = null
            this.emit('live:status', { sessionId, status: LiveStatus.IDLE })
            console.log(`[LiveSessionManager] ${sessionId} 直播已停止`)
        } catch (error: any) {
            console.error(`[LiveSessionManager] ${sessionId} 停止直播失败:`, error)
            session.error = error.message || '停止直播失败'
            this.emit('live:error', { sessionId, error, operation: 'stopLive' })
        }
    }

    /**
     * 销毁会话
     */
    async destroySession(sessionId: string): Promise<void> {
        const session = this.sessions.get(sessionId)
        if (!session) return

        // 先停止直播
        if (session.status === LiveStatus.LIVE || session.status === LiveStatus.STARTING) {
            await this.stopLive(sessionId)
        }

        // 清除该频道的 Token 缓存
        this.tokenCache.delete(session.channel)
        console.log(`[LiveSessionManager] 清除 Token 缓存: ${session.channel}`)

        // 销毁声网客户端
        if (session.agoraClient) {
            session.agoraClient.removeAllListeners()
        }

        this.sessions.delete(sessionId)
        console.log(`[LiveSessionManager] 会话已销毁: ${sessionId}`)
        this.emit('session:destroyed', { sessionId })
    }

    /**
     * 获取会话
     */
    getSession(sessionId: string): LiveSession | undefined {
        return this.sessions.get(sessionId)
    }

    /**
     * 获取所有会话
     */
    getAllSessions(): Map<string, LiveSession> {
        return this.sessions
    }

    /**
     * 获取会话状态
     */
    getSessionStatus(sessionId: string): LiveStatus {
        return this.sessions.get(sessionId)?.status || LiveStatus.IDLE
    }

    /**
     * 清除 Token 缓存
     */
    clearTokenCache(channel?: string): void {
        if (channel) {
            this.tokenCache.delete(channel)
        } else {
            this.tokenCache.clear()
        }
    }

    /**
     * 获取网络质量文本
     */
    private getQualityText(quality: number): string {
        const qualityMap: Record<number, string> = {
            1: '优秀',
            2: '良好',
            3: '一般',
            4: '较差',
            5: '很差',
            6: '未知',
        }
        return qualityMap[quality] || '未知'
    }

    // 事件系统
    on(event: string, callback: Function) {
        if (!this.eventCallbacks.has(event)) {
            this.eventCallbacks.set(event, [])
        }
        this.eventCallbacks.get(event)!.push(callback)
        // console.log(`[LiveSessionManager] 事件监听器已添加: ${event}, 当前数量: ${this.eventCallbacks.get(event)!.length}`)
    }

    off(event: string, callback?: Function) {
        if (!callback) {
            this.eventCallbacks.delete(event)
            // console.log(`[LiveSessionManager] 事件监听器已全部移除: ${event}`)
        } else {
            const callbacks = this.eventCallbacks.get(event)
            if (callbacks) {
                const index = callbacks.indexOf(callback)
                if (index > -1) {
                    callbacks.splice(index, 1)
                    // console.log(`[LiveSessionManager] 事件监听器已移除: ${event}, 剩余数量: ${callbacks.length}`)
                }
            }
        }
    }

    private emit(event: string, data: any) {
        const callbacks = this.eventCallbacks.get(event)
        if (callbacks && callbacks.length > 0) {
            // console.log(`[LiveSessionManager] 触发事件: ${event}, 监听器数量: ${callbacks.length}`)
            // 复制数组以避免在回调中修改数组导致的问题
            const callbacksCopy = [...callbacks]
            callbacksCopy.forEach((callback) => {
                try {
                    callback(data)
                } catch (error) {
                    console.error(`[LiveSessionManager] 事件回调执行失败: ${event}`, error)
                }
            })
        }
    }

    /**
     * 销毁所有会话
     */
    async destroyAll(): Promise<void> {
        const sessionIds = Array.from(this.sessions.keys())
        for (const sessionId of sessionIds) {
            await this.destroySession(sessionId)
        }
        this.tokenCache.clear()
        this.eventCallbacks.clear()
        console.log('[LiveSessionManager] 所有会话已销毁')
    }
}

// 导出单例
export const liveSessionManager = new LiveSessionManager()
