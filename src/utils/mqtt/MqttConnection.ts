/**
 * MQTT 连接管理
 * 负责底层 MQTT 连接的建立、维护和恢复
 * Requirements: 1.1, 1.2, 1.3, 8.1, 8.2, 8.3, 8.4, 8.5
 */
import mqtt, { MqttClient, IClientOptions, IPublishPacket } from 'mqtt'
import { MqttConnectionConfig, ConnectionState, ConnectionError, ConnectionErrorType, DEFAULT_CONNECTION_CONFIG } from './types'
import { ReconnectStrategy } from './ReconnectStrategy'
import { HeartbeatManager } from './HeartbeatManager'

export type ConnectionStateChangeCallback = (state: ConnectionState) => void
export type MessageCallback = (topic: string, payload: Buffer) => void

export class MqttConnection {
    private client: MqttClient | null = null
    private config: MqttConnectionConfig
    private state: ConnectionState
    private reconnectStrategy: ReconnectStrategy
    private heartbeatManager: HeartbeatManager
    private stateChangeCallbacks: ConnectionStateChangeCallback[] = []
    private messageCallbacks: MessageCallback[] = []
    private reconnectTimer: ReturnType<typeof setTimeout> | null = null
    private subscribedTopics: Map<string, number> = new Map() // topic -> qos
    private isManualDisconnect: boolean = false

    constructor(config: Partial<MqttConnectionConfig> = {}) {
        // Requirements 8.1, 8.2: 从环境变量读取配置，未设置则使用默认值
        // VITE_MQTT_HOST 格式: "host:port/path" 例如 "121.5.46.95:8083/mqtt"
        const envHost = import.meta.env.VITE_MQTT_HOST || ''

        this.config = {
            ...DEFAULT_CONNECTION_CONFIG,
            // host 包含完整的 host:port/path，用于构建 WebSocket URL
            host: envHost || config.host || '121.5.46.95:8083/mqtt',
            port: 0, // 端口已包含在 host 中，设为 0 表示不单独使用
            username: import.meta.env.VITE_MQTT_USERNAME || config.username,
            password: import.meta.env.VITE_MQTT_PASSWORD || config.password,
            ...config,
        }

        this.state = {
            status: 'disconnected',
            reconnectAttempts: 0,
        }

        this.reconnectStrategy = new ReconnectStrategy()
        this.heartbeatManager = new HeartbeatManager()

        // 设置心跳超时回调
        this.heartbeatManager.onTimeout(() => {
            console.warn('⚠️ 心跳超时，触发重连')
            this.handleDisconnect()
        })
    }

    /**
     * 连接到 MQTT 服务器
     * Requirements 1.1: 检测到连接断开后3秒内自动尝试重新连接
     */
    async connect(): Promise<boolean> {
        if (this.client?.connected) {
            return true
        }

        this.isManualDisconnect = false
        this.updateState({ status: 'connecting' })

        return new Promise((resolve) => {
            try {
                const options: IClientOptions = {
                    // 不设置 host 和 port，因为已经在 URL 中包含
                    keepalive: this.config.keepalive,
                    reconnectPeriod: 0, // 禁用内置重连，使用自定义策略
                    connectTimeout: this.config.connectTimeout,
                    clientId: this.config.clientId || `web_client_${Math.random().toString(16).slice(2, 8)}`,
                    clean: true,
                    rejectUnauthorized: false,
                }

                if (this.config.username) {
                    options.username = this.config.username
                }
                if (this.config.password) {
                    options.password = this.config.password
                }

                const url = `${this.config.host}`
                console.log('🔗 连接 MQTT 服务器...', url, 'clientId:', options.clientId, 'username:', options.username)

                this.client = mqtt.connect(url, options)

                this.client.on('connect', () => {
                    console.log('✅ MQTT 连接成功')
                    this.reconnectStrategy.reset()
                    this.updateState({
                        status: 'connected',
                        lastConnectedAt: Date.now(),
                        reconnectAttempts: 0,
                        error: undefined,
                    })

                    // 启动心跳
                    this.heartbeatManager.start()

                    // Requirements 1.3: 重连成功后恢复订阅
                    this.restoreSubscriptions()

                    resolve(true)
                })

                this.client.on('message', (topic: string, payload: Buffer, packet: IPublishPacket) => {
                    this.handleMessage(topic, payload, packet)
                })

                this.client.on('error', (error) => {
                    console.error('❌ MQTT 连接错误:', error)
                    this.handleError(error)
                    resolve(false)
                })

                this.client.on('close', () => {
                    console.log('🔌 MQTT 连接关闭')
                    if (!this.isManualDisconnect) {
                        this.handleDisconnect()
                    }
                })

                this.client.on('offline', () => {
                    console.log('📴 MQTT 离线')
                    this.updateState({ status: 'disconnected' })
                })

                // 连接超时处理
                setTimeout(() => {
                    if (!this.client?.connected && this.state.status === 'connecting') {
                        console.error('⏰ MQTT 连接超时')
                        this.handleError(new Error('连接超时'), 'timeout')
                        resolve(false)
                    }
                }, this.config.connectTimeout)
            } catch (error) {
                console.error('💥 MQTT 连接失败:', error)
                this.handleError(error as Error)
                resolve(false)
            }
        })
    }

    /**
     * 断开连接
     */
    disconnect(): void {
        this.isManualDisconnect = true
        this.heartbeatManager.stop()
        this.clearReconnectTimer()

        if (this.client) {
            this.client.end(true)
            this.client = null
        }

        this.updateState({
            status: 'disconnected',
            lastDisconnectedAt: Date.now(),
        })
    }

    /**
     * 处理断开连接
     * Requirements 1.1, 1.2: 自动重连
     */
    private handleDisconnect(): void {
        if (this.isManualDisconnect) {
            return
        }

        this.heartbeatManager.stop()
        this.updateState({
            status: 'disconnected',
            lastDisconnectedAt: Date.now(),
        })

        this.scheduleReconnect()
    }

    /**
     * 调度重连
     * Requirements 1.1: 3秒内自动尝试重新连接
     * Requirements 1.2: 失败超过3次后使用指数退避
     */
    private scheduleReconnect(): void {
        if (this.isManualDisconnect || !this.reconnectStrategy.shouldRetry()) {
            return
        }

        const delay = this.reconnectStrategy.getNextDelay()
        const attempts = this.reconnectStrategy.getAttempts()

        console.log(`🔄 ${delay / 1000}秒后尝试第 ${attempts} 次重连...`)

        this.updateState({ reconnectAttempts: attempts })

        this.reconnectTimer = setTimeout(async () => {
            console.log(`🔄 开始第 ${attempts} 次重连...`)
            const success = await this.connect()
            if (!success && !this.isManualDisconnect) {
                this.scheduleReconnect()
            }
        }, delay)
    }

    /**
     * 清除重连定时器
     */
    private clearReconnectTimer(): void {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer)
            this.reconnectTimer = null
        }
    }

    /**
     * 处理错误
     */
    private handleError(error: Error, type: ConnectionErrorType = 'unknown'): void {
        const connectionError: ConnectionError = {
            type,
            message: error.message,
            timestamp: Date.now(),
            reconnectAttempts: this.state.reconnectAttempts,
        }

        this.updateState({
            status: 'error',
            error: connectionError,
        })
    }

    /**
     * 处理消息
     */
    private handleMessage(topic: string, payload: Buffer, _packet: IPublishPacket): void {
        // 收到消息说明连接正常，重置心跳超时
        this.heartbeatManager.receiveResponse()

        // 调试：记录底层收到的消息
        // console.log('[MqttConnection] 收到消息:', topic, '回调数:', this.messageCallbacks.length)

        // 通知所有消息回调
        this.messageCallbacks.forEach((callback) => {
            try {
                callback(topic, payload)
            } catch (error) {
                console.error('消息处理回调错误:', error)
            }
        })
    }

    /**
     * 恢复订阅
     * Requirements 1.3: 重连成功后自动恢复之前的所有主题订阅
     */
    private async restoreSubscriptions(): Promise<void> {
        if (this.subscribedTopics.size === 0) {
            return
        }

        console.log(`📡 恢复 ${this.subscribedTopics.size} 个主题订阅...`)

        for (const [topic, qos] of this.subscribedTopics) {
            await this.subscribeInternal(topic, qos, false)
        }

        console.log('✅ 订阅恢复完成')
    }

    /**
     * 订阅主题
     */
    async subscribe(topic: string, qos: number = 0): Promise<boolean> {
        return this.subscribeInternal(topic, qos, true)
    }

    /**
     * 内部订阅方法
     */
    private async subscribeInternal(topic: string, qos: number, saveToList: boolean): Promise<boolean> {
        return new Promise((resolve) => {
            if (!this.client?.connected) {
                console.error('MQTT 未连接')
                resolve(false)
                return
            }

            this.client.subscribe(topic, { qos: qos as 0 | 1 | 2 }, (error) => {
                if (error) {
                    console.error('订阅失败:', error)
                    resolve(false)
                } else {
                    if (saveToList) {
                        this.subscribedTopics.set(topic, qos)
                    }
                    console.log(`✅ 订阅成功: ${topic}`)
                    resolve(true)
                }
            })
        })
    }

    /**
     * 取消订阅
     */
    async unsubscribe(topic: string): Promise<boolean> {
        return new Promise((resolve) => {
            if (!this.client?.connected) {
                console.error('MQTT 未连接')
                resolve(false)
                return
            }

            this.client.unsubscribe(topic, (error) => {
                if (error) {
                    console.error('取消订阅失败:', error)
                    resolve(false)
                } else {
                    this.subscribedTopics.delete(topic)
                    console.log(`✅ 取消订阅成功: ${topic}`)
                    resolve(true)
                }
            })
        })
    }

    /**
     * 发布消息
     */
    async publish(topic: string, message: string | Buffer, qos: number = 0, retain: boolean = false): Promise<boolean> {
        return new Promise((resolve) => {
            if (!this.client?.connected) {
                console.error('MQTT 未连接')
                resolve(false)
                return
            }

            this.client.publish(topic, message, { qos: qos as 0 | 1 | 2, retain }, (error) => {
                if (error) {
                    console.error('发布消息失败:', error)
                    resolve(false)
                } else {
                    resolve(true)
                }
            })
        })
    }

    /**
     * 获取连接状态
     */
    getState(): ConnectionState {
        return { ...this.state }
    }

    /**
     * 获取配置
     */
    getConfig(): MqttConnectionConfig {
        return { ...this.config }
    }

    /**
     * 更新配置
     * Requirements 8.3, 8.4: 运行时修改配置，断开当前连接并使用新配置重新连接
     */
    async updateConfig(config: Partial<MqttConnectionConfig>): Promise<boolean> {
        // Requirements 8.5: 配置验证
        if (config.host !== undefined && !config.host) {
            console.error('配置验证失败: host 不能为空')
            return false
        }

        const wasConnected = this.client?.connected

        // 断开当前连接
        if (wasConnected) {
            this.disconnect()
        }

        // 更新配置
        this.config = { ...this.config, ...config }

        // 重新连接
        if (wasConnected) {
            return this.connect()
        }

        return true
    }

    /**
     * 监听状态变化
     */
    onStateChange(callback: ConnectionStateChangeCallback): () => void {
        this.stateChangeCallbacks.push(callback)
        return () => {
            const index = this.stateChangeCallbacks.indexOf(callback)
            if (index > -1) {
                this.stateChangeCallbacks.splice(index, 1)
            }
        }
    }

    /**
     * 监听消息
     */
    onMessage(callback: MessageCallback): () => void {
        this.messageCallbacks.push(callback)
        return () => {
            const index = this.messageCallbacks.indexOf(callback)
            if (index > -1) {
                this.messageCallbacks.splice(index, 1)
            }
        }
    }

    /**
     * 更新状态
     */
    private updateState(partialState: Partial<ConnectionState>): void {
        this.state = { ...this.state, ...partialState }

        // 通知所有状态变化回调
        this.stateChangeCallbacks.forEach((callback) => {
            try {
                callback(this.state)
            } catch (error) {
                console.error('状态变化回调错误:', error)
            }
        })
    }

    /**
     * 获取是否已连接
     */
    isConnected(): boolean {
        return this.client?.connected || false
    }

    /**
     * 获取已订阅的主题列表
     */
    getSubscribedTopics(): string[] {
        return Array.from(this.subscribedTopics.keys())
    }

    /**
     * 清空已订阅主题列表（不取消订阅，仅清理记录）
     */
    clearSubscribedTopics(): void {
        this.subscribedTopics.clear()
        console.log('🧹 已清空 MqttConnection 订阅记录')
    }

    /**
     * 获取心跳管理器
     */
    getHeartbeatManager(): HeartbeatManager {
        return this.heartbeatManager
    }
}
