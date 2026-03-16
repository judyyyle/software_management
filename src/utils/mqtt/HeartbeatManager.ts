/**
 * 心跳管理器
 * 管理连接心跳检测
 * Requirements: 1.4, 1.5
 */
import { HeartbeatConfig, DEFAULT_HEARTBEAT_CONFIG } from './types'

export type HeartbeatSendCallback = () => void
export type HeartbeatTimeoutCallback = () => void

export class HeartbeatManager {
    private config: HeartbeatConfig
    private heartbeatTimer: ReturnType<typeof setInterval> | null = null
    private timeoutTimer: ReturnType<typeof setTimeout> | null = null
    private lastResponseTime: number = 0
    private isHealthy: boolean = true
    private sendCallback: HeartbeatSendCallback | null = null
    private timeoutCallback: HeartbeatTimeoutCallback | null = null

    constructor(config: Partial<HeartbeatConfig> = {}) {
        this.config = { ...DEFAULT_HEARTBEAT_CONFIG, ...config }
    }

    /**
     * 设置心跳发送回调
     */
    onSend(callback: HeartbeatSendCallback): void {
        this.sendCallback = callback
    }

    /**
     * 设置超时回调
     * Requirements 1.5: 心跳响应超时超过10秒时触发
     */
    onTimeout(callback: HeartbeatTimeoutCallback): () => void {
        this.timeoutCallback = callback
        return () => {
            this.timeoutCallback = null
        }
    }

    /**
     * 启动心跳
     * Requirements 1.4: 每30秒发送一次心跳消息
     */
    start(): void {
        this.stop() // 确保先停止之前的定时器
        this.isHealthy = true
        this.lastResponseTime = Date.now()

        // 立即发送一次心跳
        this.sendHeartbeat()

        // 设置定时发送心跳
        this.heartbeatTimer = setInterval(() => {
            this.sendHeartbeat()
        }, this.config.interval)
    }

    /**
     * 停止心跳
     */
    stop(): void {
        if (this.heartbeatTimer) {
            clearInterval(this.heartbeatTimer)
            this.heartbeatTimer = null
        }
        if (this.timeoutTimer) {
            clearTimeout(this.timeoutTimer)
            this.timeoutTimer = null
        }
    }

    /**
     * 发送心跳
     */
    private sendHeartbeat(): void {
        // 调用发送回调
        if (this.sendCallback) {
            this.sendCallback()
        }

        // 设置超时检测
        this.startTimeoutCheck()
    }

    /**
     * 启动超时检测
     */
    private startTimeoutCheck(): void {
        // 清除之前的超时定时器
        if (this.timeoutTimer) {
            clearTimeout(this.timeoutTimer)
        }

        // 设置新的超时定时器
        this.timeoutTimer = setTimeout(() => {
            this.handleTimeout()
        }, this.config.timeout)
    }

    /**
     * 处理超时
     * Requirements 1.5: 心跳响应超时超过10秒时主动断开并触发重连
     */
    private handleTimeout(): void {
        this.isHealthy = false
        console.warn('⚠️ 心跳超时，连接可能已断开')

        if (this.timeoutCallback) {
            this.timeoutCallback()
        }
    }

    /**
     * 收到心跳响应
     * 调用此方法表示收到了心跳响应，重置超时计时器
     */
    receiveResponse(): void {
        this.lastResponseTime = Date.now()
        this.isHealthy = true

        // 清除超时定时器
        if (this.timeoutTimer) {
            clearTimeout(this.timeoutTimer)
            this.timeoutTimer = null
        }
    }

    /**
     * 检查连接是否健康
     */
    checkHealth(): boolean {
        return this.isHealthy
    }

    /**
     * 获取最后响应时间
     */
    getLastResponseTime(): number {
        return this.lastResponseTime
    }

    /**
     * 获取配置
     */
    getConfig(): HeartbeatConfig {
        return { ...this.config }
    }

    /**
     * 更新配置
     */
    updateConfig(config: Partial<HeartbeatConfig>): void {
        this.config = { ...this.config, ...config }
    }

    /**
     * 是否正在运行
     */
    isRunning(): boolean {
        return this.heartbeatTimer !== null
    }
}
