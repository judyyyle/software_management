/**
 * MqttService 属性测试
 * Requirements: 1.3, 3.1
 *
 * 注意：由于 MqttService 依赖实际的 MQTT 连接，
 * 这里主要测试 TopicManager 的订阅恢复功能，
 * 这是 Property 2 的核心逻辑。
 */
import { describe, it, expect } from 'vitest'
import fc from 'fast-check'
import { TopicManager } from '../TopicManager'

describe('MqttService - 订阅恢复', () => {
    describe('基础功能', () => {
        it('TopicManager 应该正确初始化', () => {
            const topicManager = new TopicManager()
            expect(topicManager.getSubscriptions()).toHaveLength(0)
        })

        it('应该能够订阅主题', async () => {
            const topicManager = new TopicManager()
            topicManager.setSubscribeCallback(async () => true)

            const result = await topicManager.subscribe('test/topic', 0, 'test')
            expect(result).toBe(true)
            expect(topicManager.isSubscribed('test/topic')).toBe(true)
        })

        it('应该能够取消订阅主题', async () => {
            const topicManager = new TopicManager()
            topicManager.setSubscribeCallback(async () => true)
            topicManager.setUnsubscribeCallback(async () => true)

            await topicManager.subscribe('test/topic', 0, 'test')
            const result = await topicManager.unsubscribe('test/topic')
            expect(result).toBe(true)
            expect(topicManager.isSubscribed('test/topic')).toBe(false)
        })
    })


    /**
     * **Feature: mqtt-refactor, Property 2: 订阅恢复完整性**
     * **Validates: Requirements 1.3**
     *
     * *For any* 订阅主题集合 S，在连接断开后重连成功时，
     * 所有 S 中的主题都应该被重新订阅，且订阅后的主题集合与 S 相等。
     */
    describe('Property 2: 订阅恢复完整性', () => {
        it('订阅恢复后主题集合应该与原集合相等', async () => {
            await fc.assert(
                fc.asyncProperty(
                    // 生成随机主题列表（1-10个主题）
                    fc.array(
                        fc.record({
                            topic: fc.stringMatching(/^[a-z]+\/[a-z]+\/[a-z0-9]+$/),
                            qos: fc.integer({ min: 0, max: 2 }),
                            source: fc.stringMatching(/^[a-z]+$/),
                        }),
                        { minLength: 1, maxLength: 10 }
                    ),
                    async (subscriptions) => {
                        // 创建独立的 TopicManager 进行测试
                        const topicManager = new TopicManager()
                        const subscribedTopics: string[] = []

                        // 设置订阅回调（模拟实际订阅）
                        topicManager.setSubscribeCallback(async (topic) => {
                            subscribedTopics.push(topic)
                            return true
                        })

                        // 订阅所有主题
                        for (const sub of subscriptions) {
                            await topicManager.subscribe(sub.topic, sub.qos, sub.source)
                        }

                        // 获取订阅前的主题列表
                        const originalTopics = topicManager.getTopics()

                        // 清空已订阅记录（模拟断开连接）
                        subscribedTopics.length = 0

                        // 恢复订阅
                        await topicManager.restoreSubscriptions()

                        // 验证：恢复后的主题集合应该与原集合相等
                        const restoredTopics = new Set(subscribedTopics)
                        const originalSet = new Set(originalTopics)

                        // 检查两个集合是否相等
                        return (
                            restoredTopics.size === originalSet.size &&
                            [...originalSet].every((topic) => restoredTopics.has(topic))
                        )
                    }
                ),
                { numRuns: 100 }
            )
        })

        it('空订阅列表恢复后应该仍为空', async () => {
            const topicManager = new TopicManager()
            const subscribedTopics: string[] = []

            topicManager.setSubscribeCallback(async (topic) => {
                subscribedTopics.push(topic)
                return true
            })

            // 不订阅任何主题，直接恢复
            await topicManager.restoreSubscriptions()

            expect(subscribedTopics).toHaveLength(0)
            expect(topicManager.getTopics()).toHaveLength(0)
        })

        it('重复主题应该只恢复一次', async () => {
            await fc.assert(
                fc.asyncProperty(
                    fc.stringMatching(/^[a-z]+\/[a-z]+$/),
                    fc.integer({ min: 2, max: 5 }),
                    async (topic, repeatCount) => {
                        const topicManager = new TopicManager()
                        const subscribedTopics: string[] = []

                        topicManager.setSubscribeCallback(async (t) => {
                            subscribedTopics.push(t)
                            return true
                        })

                        // 尝试多次订阅同一主题
                        for (let i = 0; i < repeatCount; i++) {
                            await topicManager.subscribe(topic, 0, 'test')
                        }

                        // 应该只有一个订阅
                        if (topicManager.getTopics().length !== 1) {
                            return false
                        }

                        // 清空并恢复
                        subscribedTopics.length = 0
                        await topicManager.restoreSubscriptions()

                        // 恢复后也应该只有一个
                        return subscribedTopics.length === 1 && subscribedTopics[0] === topic
                    }
                ),
                { numRuns: 100 }
            )
        })
    })
})
