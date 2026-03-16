import mqtt, { MqttClient, IClientOptions, IPublishPacket } from 'mqtt'

export interface MqttConfig {
    host: string
    port: number | string
    username?: string
    password?: string
    clientId?: string
    keepalive?: number
    reconnectPeriod?: number
    connectTimeout?: number
}

export interface MqttMessage {
    topic: string
    payload: string | Buffer
    qos?: number
    retain?: boolean
}

class MqttService {
    private client: MqttClient | null = null
    config: MqttConfig
    private messageHandlers: Map<string, ((message: MqttMessage) => void)[]> = new Map()

    constructor(config: MqttConfig) {
        this.config = config
    }

    /**
     * 连接到MQTT服务器
     */
    async connect(): Promise<boolean> {
        return new Promise((resolve) => {
            try {
                const options: IClientOptions = {
                    host: this.config.host,
                    port: this.config.port,
                    keepalive: this.config.keepalive,
                    reconnectPeriod: this.config.reconnectPeriod,
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

                // 在浏览器环境中，需要使用WebSocket协议
                const url = `${this.config.host}`
                console.log('🔗 连接MQTT服务器...', url)

                this.client = mqtt.connect(url, options)

                this.client.on('connect', () => {
                    console.log('✅ MQTT连接成功')
                    // this.subscribeRequiredTopics()
                    resolve(true)
                })

                this.client.on('message', (topic: string, payload: Buffer, packet: IPublishPacket) => {
                    // console.log('📨 收到消息:', topic, JSON.parse(payload.toString()))
                    this.handleMessage(topic, payload, packet)
                })

                this.client.on('error', (error) => {
                    console.error('❌ MQTT连接错误:', error)
                    resolve(false)
                })

                this.client.on('close', () => {
                    console.log('🔌 MQTT连接关闭')
                })

                this.client.on('reconnect', () => {
                    console.log('🔄 MQTT重新连接中...')
                })

                this.client.on('offline', () => {
                    console.log('📴 MQTT离线')
                })

                // 连接超时处理
                setTimeout(() => {
                    if (!this.client?.connected) {
                        console.error('⏰ MQTT连接超时')
                        resolve(false)
                    }
                }, this.config.connectTimeout)
            } catch (error) {
                console.error('💥 MQTT连接失败:', error)
                resolve(false)
            }
        })
    }

    /**
     * 订阅几个必要的主题
     */
    subscribeRequiredTopics() {
        // this.subscribe('thing/product/7CTXN3S00B08GE/services_reply')
    }

    /**
     * 断开MQTT连接
     */
    disconnect(): void {
        if (this.client) {
            this.client.end(true)
            this.client = null
        }
    }

    /**
     * 订阅主题
     */
    subscribe(topic: string, qos: number = 0): Promise<boolean> {
        return new Promise((resolve) => {
            if (!this.client?.connected) {
                console.error('MQTT未连接')
                resolve(false)
                return
            }

            this.client.subscribe(topic, { qos }, (error) => {
                if (error) {
                    console.error('订阅失败:', error)
                    resolve(false)
                } else {
                    console.log(`✅ 订阅成功: ${topic}`)
                    resolve(true)
                }
            })
        })
    }

    /**
     * 取消订阅
     */
    unsubscribe(topic: string): Promise<boolean> {
        return new Promise((resolve) => {
            if (!this.client?.connected) {
                console.error('MQTT未连接')
                resolve(false)
                return
            }

            this.client.unsubscribe(topic, (error) => {
                if (error) {
                    console.error('取消订阅失败:', error)
                    resolve(false)
                } else {
                    console.log(`✅ 取消订阅成功: ${topic}`)
                    resolve(true)
                }
            })
        })
    }

    /**
     * 发布消息
     */
    publish(topic: string, message: string | Buffer, qos: number = 0, retain: boolean = false): Promise<boolean> {
        return new Promise((resolve) => {
            if (!this.client?.connected) {
                console.error('MQTT未连接')
                resolve(false)
                return
            }

            this.client.publish(topic, message, { qos, retain }, (error) => {
                if (error) {
                    console.error('发布消息失败:', error)
                    resolve(false)
                } else {
                    console.log(`✅ 发布成功: ${topic}`)
                    console.log(message)
                    resolve(true)
                }
            })
        })
    }

    /**
     * 添加消息处理器
     */
    onMessage(topic: string, handler: (message: MqttMessage) => void): void {
        if (!this.messageHandlers.has(topic)) {
            this.messageHandlers.set(topic, [])
        }
        this.messageHandlers.get(topic)!.push(handler)
    }

    /**
     * 移除消息处理器
     */
    offMessage(topic: string, handler?: (message: MqttMessage) => void): void {
        if (!handler) {
            this.messageHandlers.delete(topic)
        } else {
            const handlers = this.messageHandlers.get(topic)
            if (handlers) {
                const index = handlers.indexOf(handler)
                if (index > -1) {
                    handlers.splice(index, 1)
                }
            }
        }
    }

    /**
     * 处理接收到的消息
     */
    private handleMessage(topic: string, payload: Buffer, packet: IPublishPacket): void {
        const message: MqttMessage = {
            topic,
            payload: payload.toString(),
            qos: packet.qos,
            retain: packet.retain,
        }

        // 调用所有匹配的处理器
        this.messageHandlers.forEach((handlers, pattern) => {
            if (this.topicMatch(pattern, topic)) {
                handlers.forEach((handler) => {
                    try {
                        handler(message)
                    } catch (error) {
                        console.error('消息处理器错误:', error)
                    }
                })
            }
        })
    }

    /**
     * 主题匹配（支持通配符）
     */
    private topicMatch(pattern: string, topic: string): boolean {
        if (pattern === topic) return true
        if (pattern === '#') return true

        const patternParts = pattern.split('/')
        const topicParts = topic.split('/')

        for (let i = 0; i < patternParts.length; i++) {
            if (patternParts[i] === '#') return true
            if (i >= topicParts.length) return false
            if (patternParts[i] !== '+' && patternParts[i] !== topicParts[i]) return false
        }

        return patternParts.length === topicParts.length
    }

    /**
     * 获取连接状态
     */
    getConnected(): boolean {
        return this.client?.connected || false
    }

    /**
     * 获取客户端实例
     */
    getClient(): MqttClient | null {
        return this.client
    }
}

// 创建默认MQTT服务实例
const defaultMqttConfig: MqttConfig = {
    host: import.meta.env.VITE_MQTT_HOST || '121.5.46.95',
    port: import.meta.env.VITE_MQTT_HOST ? '' : '8083/mqtt',
    username: import.meta.env.VITE_MQTT_USERNAME,
    password: import.meta.env.VITE_MQTT_PASSWORD,
    clientId: `web_client_${Math.random().toString(16).slice(2, 8)}`,
    keepalive: 60,
    reconnectPeriod: 1000,
    connectTimeout: 30000,
}

export const mqttService = new MqttService(defaultMqttConfig)

export default MqttService
