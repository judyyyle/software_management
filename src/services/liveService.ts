import { ref } from 'vue'
import AgoraRTC from 'agora-rtc-sdk-ng'
import { useMqttStore } from '/@/stores/mqtt'
import { getLiveConfig, djiDockCommands, agoraEvents, LiveStatus, DeviceStatus } from '/@/config/live'
import { disposition } from '/@/config/disposition'
import { DJIoperations } from '/@/utils/mqttSdk'

/**
 * 直播服务类 - 包含重试机制
 *
 * 使用示例:
 *
 * // 1. 基本使用
 * liveService.startLive('drone')
 *
 * // 2. 自定义重试配置
 * liveService.setRetryConfig(5, 3000) // 最大重试5次，每次间隔3秒
 *
 * // 3. 监听重试事件
 * liveService.on('live:retry_attempt', (data) => {
 *   console.log(`第${data.retryCount}次重试，${data.nextRetryIn}ms后重试`)
 * })
 *
 * liveService.on('live:retry_success', (data) => {
 *   console.log(`重试成功，共重试${data.retryCount}次`)
 * })
 *
 * // 4. 监听设备状态等待事件
 * liveService.on('live:status', (data) => {
 *   if (data.status === 'waiting_mqtt') {
 *     console.log('等待MQTT连接...')
 *   } else if (data.status === 'waiting_device') {
 *     console.log('等待设备状态同步...')
 *   }
 * })
 */

export class LiveService {
    private mqttStore = useMqttStore()
    private config = getLiveConfig()

    // 声网客户端
    private agoraClient: any = null

    // 状态管理
    private liveStatus = ref<LiveStatus>(LiveStatus.IDLE)
    private deviceStatus = ref<DeviceStatus>(DeviceStatus.OFFLINE)
    private connectionQuality = ref<string>('未知')

    // 事件回调
    private eventCallbacks: Map<string, Function[]> = new Map()

    // 是否推流成功
    private isLive = ref<boolean>(false)
    // 定时器
    private timer: any = null

    // 重试机制相关
    private maxRetries = 3
    private retryDelay = 2000 // 2秒
    private currentRetryCount = 0
    private retryTimer: any = null

    constructor() {
        this.initAgoraClient()
        // // 自动订阅MQTT主题
        // this.subscribeTopics()
    }

    init() {
        this.initAgoraClient()
    }

    // 初始化声网客户端（与老版本一致，不预设角色）
    private async initAgoraClient() {
        try {
            this.agoraClient = AgoraRTC.createClient({ mode: 'live', codec: 'h264' })
            // 监听事件
            this.agoraClient.on(agoraEvents.connectionStateChange, this.handleConnectionStateChange.bind(this))
            this.agoraClient.on(agoraEvents.userPublished, this.handleUserPublished.bind(this))
            this.agoraClient.on(agoraEvents.userUnpublished, this.handleUserUnpublished.bind(this))
            this.agoraClient.on(agoraEvents.networkQuality, this.handleNetworkQuality.bind(this))
            this.agoraClient.on(agoraEvents.exception, this.handleException.bind(this))

            this.emit('agora:initialized', { success: true })
        } catch (error) {
            console.error('声网客户端初始化失败:', error)
            this.emit('agora:initialized', { success: false, error })
        }
    }

    // 检查设备状态
    private async checkDeviceStatus(): Promise<boolean> {
        try {
            // 检查MQTT连接状态
            if (!this.mqttStore.isConnected) {
                console.warn('MQTT未连接，等待连接...')
                this.emit('live:status', { status: 'waiting_mqtt', message: '等待MQTT连接...' })
                return false
            }

            // 检查设备SN是否有效
            if (!disposition.djiDock.gateway_sn) {
                console.warn('设备SN无效，等待设备状态同步...')
                this.emit('live:status', { status: 'waiting_device', message: '等待设备状态同步...' })
                return false
            }

            // 检查设备是否在线（从MQTT store中获取设备状态）
            const deviceData = this.mqttStore.deviceData
            // if (!deviceData || Object.keys(deviceData).length === 0) {
            //     console.warn('设备数据未同步，等待设备数据...')
            //     this.emit('live:status', { status: 'waiting_data', message: '等待设备数据同步...' })
            //     return false
            // }

            // 检查设备网络状态（如果有相关字段）
            // mode_code: 0=空闲, 1=现场调试, 2=远程调试, 3=固件升级, 4=作业中, 5=待标定
            if (deviceData.mode_code !== undefined && deviceData.mode_code === 0) {
                // mode_code 为 0 表示空闲，设备正常
            }

            return true
        } catch (error) {
            console.error('检查设备状态失败:', error)
            return false
        }
    }

    // 等待设备就绪
    private async waitForDeviceReady(maxWaitTime: number = 10000): Promise<boolean> {
        const startTime = Date.now()

        while (Date.now() - startTime < maxWaitTime) {
            if (await this.checkDeviceStatus()) {
                return true
            }
            await new Promise((resolve) => setTimeout(resolve, 1000)) // 等待1秒
        }

        return false
    }

    // 带重试的直播启动（简化，与老版本一致）
    private async startLiveWithRetry(type: string): Promise<void> {
        this.currentRetryCount = 0

        try {
            // 发送MQTT命令开始直播
            if (type === 'cabin') {
                await DJIoperations.sendServices(disposition.djiDock.gateway_sn, 'live_start_push', disposition.getDjiDockData())
            } else {
                await DJIoperations.sendServices(disposition.djiDock.gateway_sn, 'live_start_push', disposition.getDeviceData())
            }

            // 订阅服务回复
            DJIoperations.deviceServicesReply(disposition.djiDock.gateway_sn)

            // 加入声网频道（添加连接状态检查，防止重复加入）
            if (this.agoraClient) {
                const connectionState = this.agoraClient.connectionState
                if (connectionState === 'CONNECTED' || connectionState === 'CONNECTING') {
                    console.log('Agora 客户端已连接或正在连接，跳过加入频道')
                } else {
                    // 【关键】设置角色为观众（audience），才能接收视频流
                    await this.agoraClient.setClientRole('audience')
                    console.log('Agora 客户端角色已设置为 audience')

                    await this.agoraClient.join(
                        (disposition as any)[type].appId,
                        (disposition as any)[type].channel,
                        (disposition as any)[type].token,
                        (disposition as any)[type].uid
                    )
                    this.emit('agora:joined', {
                        channel: (disposition as any)[type].channel,
                        uid: (disposition as any)[type].uid,
                    })
                }
            }

            // 重置重试计数
            this.currentRetryCount = 0
            this.emit('live:retry_success', { retryCount: this.currentRetryCount })
        } catch (error) {
            console.error(`直播启动失败:`, error)

            this.liveStatus.value = LiveStatus.ERROR
            this.emit('live:error', {
                error,
                operation: '开始直播',
                name: (disposition as any)[type].channel,
            })
        }
    }

    // 开始直播
    async startLive(type: string) {
        try {
            this.liveStatus.value = LiveStatus.STARTING
            this.emit('live:status', { status: LiveStatus.STARTING })

            // 等待设备就绪
            const deviceReady = await this.waitForDeviceReady()
            if (!deviceReady) {
                throw new Error('设备未就绪，请检查设备状态')
            }

            // 确保 Token 已获取
            if (!(disposition as any)[type]?.token) {
                console.log('Token 为空，正在获取...')
                await disposition.refreshTokens()
            }

            // 使用重试机制启动直播
            await this.startLiveWithRetry(type)
        } catch (error) {
            console.error('开始直播失败:', error)
            this.liveStatus.value = LiveStatus.ERROR
            this.emit('live:error', { error, operation: 'startLive' })
        }
    }

    // 停止直播
    async stopLive(type: string) {
        try {
            this.liveStatus.value = LiveStatus.STOPPING
            this.emit('live:status', { status: LiveStatus.STOPPING })

            // 清除重试定时器
            if (this.retryTimer) {
                clearTimeout(this.retryTimer)
                this.retryTimer = null
            }

            // 离开声网频道
            if (this.agoraClient) {
                await this.agoraClient.leave()
                this.emit('agora:left', { channel: (disposition as any)[type].channel })
            }

            this.liveStatus.value = LiveStatus.IDLE
            if (type === 'cabin') {
                await DJIoperations.sendServices(disposition.djiDock.gateway_sn, 'live_stop_push', { video_id: disposition.djiDock.videoId })
            } else {
                await DJIoperations.sendServices(disposition.djiDock.gateway_sn, 'live_stop_push', { video_id: disposition.device.videoId })
            }
            DJIoperations.deviceServicesReplyClose(disposition.djiDock.gateway_sn)

            this.emit('live:status', { status: LiveStatus.IDLE })
        } catch (error) {
            console.error('停止直播失败:', error)
            this.emit('live:error', { error, operation: 'stopLive' })
        }
    }

    // 切换相机
    async switchCamera() {
        try {
            await this.mqttStore.publish(this.config.djiDock.topics.services, JSON.stringify(djiDockCommands.live_camera_change()))
        } catch (error) {
            console.error('切换相机失败:', error)
        }
    }
    // 获取设备状态
    async getDeviceStatus() {
        try {
            await this.mqttStore.publish(this.config.djiDock.topics.services, JSON.stringify(djiDockCommands.getStatus))

            this.emit('mqtt:command', {
                command: 'getStatus',
                topic: this.config.djiDock.topics.services,
            })
        } catch (error) {
            console.error('获取设备状态失败:', error)
            this.emit('live:error', { error, operation: 'getStatus' })
        }
    }

    // 重启设备
    async restartDevice() {
        try {
            await this.mqttStore.publish(this.config.djiDock.topics.services, JSON.stringify(djiDockCommands.restart))

            this.emit('mqtt:command', {
                command: 'restart',
                topic: this.config.djiDock.topics.services,
            })
        } catch (error) {
            console.error('重启设备失败:', error)
            this.emit('live:error', { error, operation: 'restart' })
        }
    }

    // 处理声网连接状态变化
    private handleConnectionStateChange(curState: string, prevState: string) {
        this.emit('agora:connectionState', { current: curState, previous: prevState })

        if (curState === 'CONNECTED') {
            this.connectionQuality.value = '良好'
        } else if (curState === 'DISCONNECTED') {
            this.connectionQuality.value = '断开'
        }
    }

    // 处理用户发布
    private async handleUserPublished(user: any, mediaType: string) {
        try {
            await this.agoraClient.subscribe(user, mediaType)
            if (mediaType === 'video') {
                this.isLive.value = true
                if (this.timer) {
                    clearInterval(this.timer)
                }
                this.emit('agora:videoTrack', { user, track: user.videoTrack })
            } else if (mediaType === 'audio') {
                this.emit('agora:audioTrack', { user, track: user.audioTrack })
            }

            this.emit('agora:userPublished', { user, mediaType })
        } catch (error) {
            console.error('订阅失败:', error)
            this.emit('agora:error', { error, operation: 'subscribe' })
        }
    }

    // 处理用户取消发布
    private handleUserUnpublished(user: any) {
        this.emit('agora:userUnpublished', { user })
    }

    // 处理网络质量
    private handleNetworkQuality(stats: any) {
        this.connectionQuality.value = this.getQualityText(stats.downlinkNetworkQuality)
        this.emit('agora:networkQuality', stats)
    }

    // 处理异常
    private handleException(exception: any) {
        console.error('声网异常:', exception)
        this.emit('agora:exception', exception)

        // 处理视频解码失败错误 (code: 1005)
        if (exception.code === 1005 || exception.msg === 'RECV_VIDEO_DECODE_FAILED') {
            console.warn('视频解码失败，可能是编解码器不匹配或视频流异常')
            this.emit('live:error', {
                error: '视频解码失败，请检查推流设备状态',
                operation: 'video_decode',
                code: 1005,
            })
        }
    }

    // 处理MQTT消息
    handleMqttMessage(topic: string, message: string) {
        try {
            const data = JSON.parse(message)
            this.emit('mqtt:message', { topic, data })

            // 处理设备状态数据 - 由于配置中注释了osd主题，这里只处理services
            if (topic === this.config.djiDock.topics.services) {
                this.handleServicesMessage(data)
            }
        } catch (error) {
            console.error('解析MQTT消息失败:', error)
            this.emit('mqtt:error', { error, topic, message })
        }
    }

    // 处理服务消息
    private handleServicesMessage(data: any) {
        // 处理服务响应消息
        if (data.result === 'success') {
            if (data.service === 'startLive') {
                this.liveStatus.value = LiveStatus.LIVE
                this.emit('live:status', { status: LiveStatus.LIVE })
            } else if (data.service === 'stopLive') {
                this.liveStatus.value = LiveStatus.IDLE
                this.emit('live:status', { status: LiveStatus.IDLE })
            }
        }

        this.emit('services:response', data)
    }

    // 获取网络质量文本
    private getQualityText(quality: number): string {
        switch (quality) {
            case 1:
                return '优秀'
            case 2:
                return '良好'
            case 3:
                return '一般'
            case 4:
                return '较差'
            case 5:
                return '很差'
            case 6:
                return '未知'
            default:
                return '未知'
        }
    }

    // 事件监听
    on(event: string, callback: Function) {
        if (!this.eventCallbacks.has(event)) {
            this.eventCallbacks.set(event, [])
        }
        this.eventCallbacks.get(event)!.push(callback)
    }

    // 事件移除
    off(event: string, callback?: Function) {
        if (!callback) {
            this.eventCallbacks.delete(event)
        } else {
            const callbacks = this.eventCallbacks.get(event)
            if (callbacks) {
                const index = callbacks.indexOf(callback)
                if (index > -1) {
                    callbacks.splice(index, 1)
                }
            }
        }
    }

    // 事件触发
    private emit(event: string, data: any) {
        const callbacks = this.eventCallbacks.get(event)
        if (callbacks) {
            callbacks.forEach((callback) => callback(data))
        }
    }

    // 获取状态
    getStatus() {
        return {
            liveStatus: this.liveStatus.value,
            deviceStatus: this.deviceStatus.value,
            connectionQuality: this.connectionQuality.value,
            retryInfo: {
                currentRetryCount: this.currentRetryCount,
                maxRetries: this.maxRetries,
                retryDelay: this.retryDelay,
            },
        }
    }

    // 设置重试配置
    setRetryConfig(maxRetries: number, retryDelay: number) {
        this.maxRetries = maxRetries
        this.retryDelay = retryDelay
        console.log(`重试配置已更新: 最大重试次数=${maxRetries}, 重试延迟=${retryDelay}ms`)
    }

    // 重置重试状态
    resetRetryState() {
        this.currentRetryCount = 0
        if (this.retryTimer) {
            clearTimeout(this.retryTimer)
            this.retryTimer = null
        }
        console.log('重试状态已重置')
    }

    // 检查是否正在重试
    isRetrying(): boolean {
        return this.currentRetryCount > 0 && this.currentRetryCount <= this.maxRetries
    }

    // 获取重试信息
    getRetryInfo() {
        return {
            currentRetryCount: this.currentRetryCount,
            maxRetries: this.maxRetries,
            retryDelay: this.retryDelay,
            isRetrying: this.isRetrying(),
        }
    }

    // 销毁（与老版本一致）
    destroy() {
        if (this.agoraClient) {
            this.agoraClient.leave()
        }
        // 清除重试定时器
        if (this.retryTimer) {
            clearTimeout(this.retryTimer)
            this.retryTimer = null
        }
        this.eventCallbacks.clear()
    }
}

// 创建单例实例（与老版本一致）
export const liveService = new LiveService()
