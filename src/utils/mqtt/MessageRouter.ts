/**
 * 消息路由器
 * 将消息分发给注册的处理器
 * Requirements: 5.1, 5.2, 5.3, 5.4, 5.5
 */
import { MessageHandler, MessageHandlerRegistration } from './types'

export class MessageRouter {
    private handlers: Map<string, MessageHandlerRegistration> = new Map()
    private idCounter: number = 0

    /**
     * 注册消息处理器
     * Requirements 5.2: 支持按主题模式（包含通配符）进行过滤
     * Requirements 5.4: 提供取消注册的接口（返回 id）
     */
    register(pattern: string, handler: MessageHandler, id?: string): string {
        const handlerId = id || `handler_${++this.idCounter}`

        this.handlers.set(handlerId, {
            id: handlerId,
            pattern,
            handler,
        })

        return handlerId
    }

    /**
     * 取消注册消息处理器
     * Requirements 5.4: 提供取消注册的接口
     */
    unregister(id: string): boolean {
        return this.handlers.delete(id)
    }

    /**
     * 路由消息到匹配的处理器
     * Requirements 5.1: 将消息分发给所有注册的 Message_Handler
     * Requirements 5.3: 捕获异常并记录错误日志而不影响其他处理器
     */
    route(topic: string, payload: Buffer | string): void {
        const payloadData = this.parsePayload(payload)

        // 调试：记录路由信息
        // console.log('[MessageRouter] 路由消息:', topic, '处理器数:', this.handlers.size)

        let matched = false
        for (const [, registration] of this.handlers) {
            if (this.topicMatch(registration.pattern, topic)) {
                matched = true
                try {
                    registration.handler(topic, payloadData)
                } catch (error) {
                    // Requirements 5.3: 捕获异常并记录错误日志
                    console.error(`消息处理器 [${registration.id}] 错误:`, error)
                }
            }
        }

        if (!matched) {
            console.warn('[MessageRouter] 无匹配处理器:', topic)
        }
    }

    /**
     * 解析消息负载
     */
    private parsePayload(payload: Buffer | string): any {
        const str = typeof payload === 'string' ? payload : payload.toString()

        try {
            return JSON.parse(str)
        } catch {
            return str
        }
    }

    /**
     * 主题匹配（支持 MQTT 通配符）
     * Requirements 5.2: 支持按主题模式（包含通配符）进行过滤
     * - # 匹配任意层级
     * - + 匹配单个层级
     */
    topicMatch(pattern: string, topic: string): boolean {
        // 精确匹配
        if (pattern === topic) return true

        // # 匹配所有
        if (pattern === '#') return true

        const patternParts = pattern.split('/')
        const topicParts = topic.split('/')

        for (let i = 0; i < patternParts.length; i++) {
            const patternPart = patternParts[i]

            // # 匹配剩余所有层级
            if (patternPart === '#') {
                return true
            }

            // 主题层级不足
            if (i >= topicParts.length) {
                return false
            }

            // + 匹配单个层级
            if (patternPart === '+') {
                continue
            }

            // 精确匹配当前层级
            if (patternPart !== topicParts[i]) {
                return false
            }
        }

        // 层级数量必须相等（除非使用了 #）
        return patternParts.length === topicParts.length
    }

    /**
     * 获取所有注册的处理器
     */
    getHandlers(): MessageHandlerRegistration[] {
        return Array.from(this.handlers.values())
    }

    /**
     * 获取处理器数量
     */
    getCount(): number {
        return this.handlers.size
    }

    /**
     * 检查处理器是否存在
     */
    hasHandler(id: string): boolean {
        return this.handlers.has(id)
    }

    /**
     * 清空所有处理器
     */
    clear(): void {
        this.handlers.clear()
    }

    /**
     * 获取匹配指定主题的处理器
     */
    getMatchingHandlers(topic: string): MessageHandlerRegistration[] {
        const matching: MessageHandlerRegistration[] = []

        for (const [, registration] of this.handlers) {
            if (this.topicMatch(registration.pattern, topic)) {
                matching.push(registration)
            }
        }

        return matching
    }
}
