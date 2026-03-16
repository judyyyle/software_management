/**
 * 重连策略
 * 实现指数退避算法
 * Requirements: 1.1, 1.2
 */
import { ReconnectStrategyConfig, DEFAULT_RECONNECT_CONFIG } from './types'

export class ReconnectStrategy {
    private config: ReconnectStrategyConfig
    private attempts: number = 0

    constructor(config: Partial<ReconnectStrategyConfig> = {}) {
        this.config = { ...DEFAULT_RECONNECT_CONFIG, ...config }
    }

    /**
     * 获取下一次重连延迟时间
     * 前3次使用初始延迟，之后使用指数退避
     * Requirements 1.2: 重连失败超过3次后以指数退避策略继续重试
     */
    getNextDelay(): number {
        this.attempts++

        // 前3次使用初始延迟（3秒）
        if (this.attempts <= 3) {
            return this.config.initialDelay
        }

        // 第4次开始使用指数退避：5秒、10秒、20秒...
        // 计算指数退避延迟：baseDelay * multiplier^(attempts-4)
        // 其中 baseDelay = 5000ms (5秒)
        const baseDelay = 5000
        const exponent = this.attempts - 4
        const delay = baseDelay * Math.pow(this.config.backoffMultiplier, exponent)

        return Math.min(delay, this.config.maxDelay)
    }

    /**
     * 重置重连计数
     */
    reset(): void {
        this.attempts = 0
    }

    /**
     * 是否应该继续重试
     */
    shouldRetry(): boolean {
        if (this.config.maxAttempts === 0) {
            return true // 无限重试
        }
        return this.attempts < this.config.maxAttempts
    }

    /**
     * 获取当前重连次数
     */
    getAttempts(): number {
        return this.attempts
    }

    /**
     * 获取配置
     */
    getConfig(): ReconnectStrategyConfig {
        return { ...this.config }
    }

    /**
     * 更新配置
     */
    updateConfig(config: Partial<ReconnectStrategyConfig>): void {
        this.config = { ...this.config, ...config }
    }
}
