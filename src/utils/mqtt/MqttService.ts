/**
 * MQTT 服务层
 * 整合 MqttConnection、TopicManager、MessageRouter、RequestTracker
 * 提供统一的服务接口
 * Requirements: 1.3, 3.1
 */
import { MqttConnection } from './MqttConnection'
import { TopicManager } from './TopicManager'
import { MessageRouter } from './MessageRouter'
import { RequestTracker } from './RequestTracker'
import {
    MqttConnectionConfig,
    ConnectionState,
    TopicSubscription,
    MessageHandler,
    TopicPatterns,
} from './types'

export class MqttService {
    private connection: MqttConnection
    private topicManager: TopicManager
    private messageRouter: MessageRouter
    private requestTracker: RequestTracker
    private unsubscribeMessageHandler: (() => void) | null = null

    constructor(config?: Partial<MqttConnectionConfig>) {
        this.connection = new MqttConnection(config)
        this.topicManager = new TopicManager()
        this.messageRouter = new MessageRouter()
        this.requestTracker = new RequestTracker()

        this.setupIntegration()
    }

    /**
     * 设置组件集成
     */
    private setupIntegration(): void {
        // 设置 TopicManager 的订阅/取消订阅回调
        this.topicManager.setSubscribeCallback(async (topic, qos) => {
            return this.connection.subscribe(topic, qos)
        })

        this.topicManager.setUnsubscribeCallback(async (topic) => {
            return this.connection.unsubscribe(topic)
        })

        // 监听连接消息，路由到 MessageRouter
        this.unsubscribeMessageHandler = this.connection.onMessage((topic, payload) => {
            this.messageRouter.route(topic, payload)
        })

        // 监听连接状态变化，处理重连后的订阅恢复
        this.connection.onStateChange((state) => {
            if (state.status === 'connected' && state.reconnectAttempts > 0) {
                // Requirements 1.3: 重连成功后恢复订阅
                this.restoreSubscriptions()
            }
        })
    }


    // ==================== 连接管理 ====================

    /**
     * 连接到 MQTT 服务器
     */
    async connect(): Promise<boolean> {
        return this.connection.connect()
    }

    /**
     * 断开连接
     */
    disconnect(): void {
        this.requestTracker.cancelAll()
        this.connection.disconnect()
    }

    /**
     * 获取连接状态
     */
    getConnectionState(): ConnectionState {
        return this.connection.getState()
    }

    /**
     * 检查是否已连接
     */
    isConnected(): boolean {
        return this.connection.isConnected()
    }

    /**
     * 更新配置
     */
    async updateConfig(config: Partial<MqttConnectionConfig>): Promise<boolean> {
        return this.connection.updateConfig(config)
    }

    // ==================== 主题管理 ====================

    /**
     * 订阅主题
     * Requirements 3.1: 通过统一接口进行订阅
     */
    async subscribe(topic: string, qos: number = 0, source: string = 'default'): Promise<boolean> {
        return this.topicManager.subscribe(topic, qos, source)
    }

    /**
     * 取消订阅
     */
    async unsubscribe(topic: string): Promise<boolean> {
        return this.topicManager.unsubscribe(topic)
    }

    /**
     * 按来源取消订阅
     */
    async unsubscribeBySource(source: string): Promise<void> {
        return this.topicManager.unsubscribeBySource(source)
    }

    /**
     * 获取所有订阅
     */
    getSubscriptions(): TopicSubscription[] {
        return this.topicManager.getSubscriptions()
    }

    /**
     * 检查主题是否已订阅
     */
    isSubscribed(topic: string): boolean {
        return this.topicManager.isSubscribed(topic)
    }

    /**
     * 恢复订阅
     * Requirements 1.3: 重连成功后自动恢复之前的所有主题订阅
     */
    private async restoreSubscriptions(): Promise<void> {
        console.log('📡 恢复订阅...')
        await this.topicManager.restoreSubscriptions()
        console.log('✅ 订阅恢复完成')
    }

    // ==================== 消息处理 ====================

    /**
     * 注册消息处理器
     * @param pattern 主题模式（支持 MQTT 通配符）
     * @param handler 消息处理函数
     * @param id 可选的处理器 ID，用于避免重复注册
     */
    onMessage(pattern: string, handler: MessageHandler, id?: string): () => void {
        const handlerId = this.messageRouter.register(pattern, handler, id)
        return () => {
            this.messageRouter.unregister(handlerId)
        }
    }

    /**
     * 路由消息（用于测试或手动触发）
     */
    routeMessage(topic: string, payload: Buffer | string): void {
        this.messageRouter.route(topic, payload)
    }


    // ==================== 发布消息 ====================

    /**
     * 发布消息
     */
    async publish(topic: string, message: any, qos: number = 0): Promise<boolean> {
        const payload = typeof message === 'string' ? message : JSON.stringify(message)
        return this.connection.publish(topic, payload, qos)
    }

    // ==================== 服务请求 ====================

    /**
     * 发送服务请求（带响应追踪）
     * Requirements 9.1, 9.2, 9.3, 9.4
     */
    async request(sn: string, method: string, data: any = {}, timeout?: number): Promise<any> {
        const tid = this.requestTracker.generateId()
        const bid = this.requestTracker.generateId()

        const message = {
            tid,
            bid,
            method,
            data,
            timestamp: Date.now(),
        }

        // 订阅响应主题（如果尚未订阅）
        const replyTopic = TopicPatterns.SERVICES_REPLY(sn)
        if (!this.isSubscribed(replyTopic)) {
            await this.subscribe(replyTopic, 0, 'service-request')

            // 注册响应处理器
            this.onMessage(replyTopic, (topic, payload) => {
                if (payload?.tid) {
                    this.requestTracker.resolve(payload.tid, payload)
                }
            })
        }

        // 发布请求
        const serviceTopic = TopicPatterns.SERVICES(sn)
        const published = await this.publish(serviceTopic, message, 1)

        if (!published) {
            throw new Error('发送请求失败')
        }

        // 追踪请求并等待响应
        return this.requestTracker.track(tid, bid, method, timeout)
    }

    /**
     * 取消所有待处理请求
     */
    cancelAllRequests(): void {
        this.requestTracker.cancelAll()
    }

    /**
     * 获取待处理请求数量
     */
    getPendingRequestCount(): number {
        return this.requestTracker.getPendingCount()
    }

    // ==================== 事件监听 ====================

    /**
     * 监听连接状态变化
     */
    onConnectionChange(callback: (state: ConnectionState) => void): () => void {
        return this.connection.onStateChange(callback)
    }

    // ==================== 组件访问 ====================

    /**
     * 获取 MQTT 配置
     */
    getConfig(): MqttConnectionConfig {
        return this.connection.getConfig()
    }

    /**
     * 获取连接实例（用于高级操作）
     */
    getConnection(): MqttConnection {
        return this.connection
    }

    /**
     * 获取主题管理器（用于高级操作）
     */
    getTopicManager(): TopicManager {
        return this.topicManager
    }

    /**
     * 获取消息路由器（用于高级操作）
     */
    getMessageRouter(): MessageRouter {
        return this.messageRouter
    }

    /**
     * 获取请求追踪器（用于高级操作）
     */
    getRequestTracker(): RequestTracker {
        return this.requestTracker
    }

    // ==================== 清理 ====================

    /**
     * 销毁服务
     */
    destroy(): void {
        if (this.unsubscribeMessageHandler) {
            this.unsubscribeMessageHandler()
        }
        this.requestTracker.cancelAll()
        this.messageRouter.clear()
        this.topicManager.clear()
        this.connection.disconnect()
    }
}

// 创建单例实例
let mqttServiceInstance: MqttService | null = null

/**
 * 获取 MqttService 单例
 */
export function getMqttService(config?: Partial<MqttConnectionConfig>): MqttService {
    if (!mqttServiceInstance) {
        mqttServiceInstance = new MqttService(config)
    }
    return mqttServiceInstance
}

/**
 * 重置 MqttService 单例（用于测试）
 */
export function resetMqttService(): void {
    if (mqttServiceInstance) {
        mqttServiceInstance.destroy()
        mqttServiceInstance = null
    }
}
