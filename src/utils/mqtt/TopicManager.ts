/**
 * 主题管理器
 * 集中管理所有主题订阅
 * Requirements: 3.1, 3.2, 3.3, 3.4, 3.5
 */
import { TopicSubscription } from './types'

export type SubscribeCallback = (topic: string, qos: number) => Promise<boolean>
export type UnsubscribeCallback = (topic: string) => Promise<boolean>

export interface SubscriptionLog {
    action: 'subscribe' | 'unsubscribe'
    topic: string
    timestamp: number
    source?: string
    success: boolean
}

export class TopicManager {
    private subscriptions: Map<string, TopicSubscription> = new Map()
    private subscribeCallback: SubscribeCallback | null = null
    private unsubscribeCallback: UnsubscribeCallback | null = null
    private logs: SubscriptionLog[] = []
    private maxLogSize: number = 100

    /**
     * 设置订阅回调
     */
    setSubscribeCallback(callback: SubscribeCallback): void {
        this.subscribeCallback = callback
    }

    /**
     * 设置取消订阅回调
     */
    setUnsubscribeCallback(callback: UnsubscribeCallback): void {
        this.unsubscribeCallback = callback
    }

    /**
     * 订阅主题
     * Requirements 3.2: 检查该主题是否已订阅以避免重复订阅
     * Requirements 3.5: 记录操作日志
     */
    async subscribe(topic: string, qos: number = 0, source: string = 'default'): Promise<boolean> {
        // Requirements 3.2: 检查是否已订阅
        if (this.subscriptions.has(topic)) {
            console.log(`📌 主题已订阅，跳过: ${topic}`)
            return true
        }

        let success = true

        // 调用实际订阅回调
        if (this.subscribeCallback) {
            success = await this.subscribeCallback(topic, qos)
        }

        if (success) {
            // 记录订阅信息
            this.subscriptions.set(topic, {
                topic,
                qos,
                subscribedAt: Date.now(),
                source,
            })
        }

        // Requirements 3.5: 记录操作日志
        this.addLog({
            action: 'subscribe',
            topic,
            timestamp: Date.now(),
            source,
            success,
        })

        return success
    }

    /**
     * 取消订阅
     * Requirements 3.3: 验证该主题确实存在于订阅列表中
     * Requirements 3.5: 记录操作日志
     */
    async unsubscribe(topic: string): Promise<boolean> {
        // Requirements 3.3: 验证主题是否存在
        if (!this.subscriptions.has(topic)) {
            console.warn(`⚠️ 主题未订阅，无法取消: ${topic}`)
            this.addLog({
                action: 'unsubscribe',
                topic,
                timestamp: Date.now(),
                success: false,
            })
            return false
        }

        let success = true

        // 调用实际取消订阅回调
        if (this.unsubscribeCallback) {
            success = await this.unsubscribeCallback(topic)
        }

        if (success) {
            this.subscriptions.delete(topic)
        }

        // Requirements 3.5: 记录操作日志
        this.addLog({
            action: 'unsubscribe',
            topic,
            timestamp: Date.now(),
            success,
        })

        return success
    }

    /**
     * 按来源取消订阅
     */
    async unsubscribeBySource(source: string): Promise<void> {
        const topicsToUnsubscribe: string[] = []

        for (const [topic, subscription] of this.subscriptions) {
            if (subscription.source === source) {
                topicsToUnsubscribe.push(topic)
            }
        }

        for (const topic of topicsToUnsubscribe) {
            await this.unsubscribe(topic)
        }
    }

    /**
     * 检查主题是否已订阅
     */
    isSubscribed(topic: string): boolean {
        return this.subscriptions.has(topic)
    }

    /**
     * 获取所有订阅
     * Requirements 3.4: 返回当前所有已订阅主题的完整列表
     */
    getSubscriptions(): TopicSubscription[] {
        return Array.from(this.subscriptions.values())
    }

    /**
     * 获取订阅的主题列表
     */
    getTopics(): string[] {
        return Array.from(this.subscriptions.keys())
    }

    /**
     * 按来源获取订阅
     */
    getSubscriptionsBySource(source: string): TopicSubscription[] {
        return Array.from(this.subscriptions.values()).filter((sub) => sub.source === source)
    }

    /**
     * 获取订阅数量
     */
    getCount(): number {
        return this.subscriptions.size
    }

    /**
     * 清空所有订阅（不调用取消订阅回调）
     */
    clear(): void {
        this.subscriptions.clear()
    }

    /**
     * 添加日志
     */
    private addLog(log: SubscriptionLog): void {
        this.logs.push(log)

        // 限制日志数量
        if (this.logs.length > this.maxLogSize) {
            this.logs.shift()
        }
    }

    /**
     * 获取日志
     */
    getLogs(): SubscriptionLog[] {
        return [...this.logs]
    }

    /**
     * 清空日志
     */
    clearLogs(): void {
        this.logs = []
    }

    /**
     * 恢复订阅（用于重连后）
     */
    async restoreSubscriptions(): Promise<void> {
        if (!this.subscribeCallback) {
            return
        }

        const subscriptions = Array.from(this.subscriptions.values())

        for (const sub of subscriptions) {
            await this.subscribeCallback(sub.topic, sub.qos)
        }
    }
}
