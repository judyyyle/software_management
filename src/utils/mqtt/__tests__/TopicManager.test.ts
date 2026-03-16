/**
 * TopicManager 测试
 * **Feature: mqtt-refactor, Property 4: 订阅管理一致性**
 * **Validates: Requirements 3.2, 3.3, 3.4**
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import fc from 'fast-check'
import { TopicManager } from '../TopicManager'

describe('TopicManager', () => {
    let manager: TopicManager

    beforeEach(() => {
        manager = new TopicManager()
        // 设置模拟回调
        manager.setSubscribeCallback(async () => true)
        manager.setUnsubscribeCallback(async () => true)
    })

    describe('基础功能', () => {
        it('初始订阅数量应该为0', () => {
            expect(manager.getCount()).toBe(0)
        })

        it('subscribe 应该添加订阅', async () => {
            await manager.subscribe('test/topic', 0, 'test')

            expect(manager.isSubscribed('test/topic')).toBe(true)
            expect(manager.getCount()).toBe(1)
        })

        it('unsubscribe 应该移除订阅', async () => {
            await manager.subscribe('test/topic', 0, 'test')
            await manager.unsubscribe('test/topic')

            expect(manager.isSubscribed('test/topic')).toBe(false)
            expect(manager.getCount()).toBe(0)
        })

        it('重复订阅同一主题应该跳过', async () => {
            await manager.subscribe('test/topic', 0, 'test')
            await manager.subscribe('test/topic', 0, 'test')

            expect(manager.getCount()).toBe(1)
        })

        it('取消订阅不存在的主题应该返回 false', async () => {
            const result = await manager.unsubscribe('nonexistent/topic')
            expect(result).toBe(false)
        })

        it('getSubscriptions 应该返回所有订阅', async () => {
            await manager.subscribe('topic1', 0, 'source1')
            await manager.subscribe('topic2', 1, 'source2')

            const subscriptions = manager.getSubscriptions()
            expect(subscriptions.length).toBe(2)
            expect(subscriptions.map((s) => s.topic)).toContain('topic1')
            expect(subscriptions.map((s) => s.topic)).toContain('topic2')
        })

        it('getSubscriptionsBySource 应该按来源过滤', async () => {
            await manager.subscribe('topic1', 0, 'source1')
            await manager.subscribe('topic2', 0, 'source1')
            await manager.subscribe('topic3', 0, 'source2')

            const source1Subs = manager.getSubscriptionsBySource('source1')
            expect(source1Subs.length).toBe(2)

            const source2Subs = manager.getSubscriptionsBySource('source2')
            expect(source2Subs.length).toBe(1)
        })

        it('unsubscribeBySource 应该取消指定来源的所有订阅', async () => {
            await manager.subscribe('topic1', 0, 'source1')
            await manager.subscribe('topic2', 0, 'source1')
            await manager.subscribe('topic3', 0, 'source2')

            await manager.unsubscribeBySource('source1')

            expect(manager.getCount()).toBe(1)
            expect(manager.isSubscribed('topic3')).toBe(true)
        })
    })

    describe('日志功能', () => {
        it('订阅操作应该记录日志', async () => {
            await manager.subscribe('test/topic', 0, 'test')

            const logs = manager.getLogs()
            expect(logs.length).toBe(1)
            expect(logs[0].action).toBe('subscribe')
            expect(logs[0].topic).toBe('test/topic')
            expect(logs[0].success).toBe(true)
        })

        it('取消订阅操作应该记录日志', async () => {
            await manager.subscribe('test/topic', 0, 'test')
            await manager.unsubscribe('test/topic')

            const logs = manager.getLogs()
            expect(logs.length).toBe(2)
            expect(logs[1].action).toBe('unsubscribe')
        })

        it('日志应该限制数量', async () => {
            // 订阅超过100个主题
            for (let i = 0; i < 150; i++) {
                await manager.subscribe(`topic${i}`, 0, 'test')
            }

            const logs = manager.getLogs()
            expect(logs.length).toBeLessThanOrEqual(100)
        })
    })

    describe('Property 4: 订阅管理一致性', () => {
        /**
         * **Feature: mqtt-refactor, Property 4: 订阅管理一致性**
         * **Validates: Requirements 3.2, 3.3, 3.4**
         *
         * 对于任意主题订阅操作序列，订阅列表应该准确反映当前实际订阅的主题
         */
        it('重复订阅同一主题不会产生重复条目', () => {
            fc.assert(
                fc.asyncProperty(
                    fc.array(fc.string({ minLength: 1, maxLength: 50 }), { minLength: 1, maxLength: 20 }),
                    async (topics) => {
                        const testManager = new TopicManager()
                        testManager.setSubscribeCallback(async () => true)

                        // 订阅所有主题（可能有重复）
                        for (const topic of topics) {
                            await testManager.subscribe(topic, 0, 'test')
                        }

                        // 获取唯一主题数
                        const uniqueTopics = new Set(topics)
                        const subscriptions = testManager.getSubscriptions()

                        // 订阅数量应该等于唯一主题数
                        expect(subscriptions.length).toBe(uniqueTopics.size)

                        // 每个主题只应该出现一次
                        const subscribedTopics = subscriptions.map((s) => s.topic)
                        const subscribedSet = new Set(subscribedTopics)
                        expect(subscribedSet.size).toBe(subscribedTopics.length)
                    }
                ),
                { numRuns: 100 }
            )
        })

        it('取消订阅后主题从列表中移除', () => {
            fc.assert(
                fc.asyncProperty(
                    fc.array(fc.string({ minLength: 1, maxLength: 50 }), { minLength: 1, maxLength: 10 }),
                    async (topics) => {
                        const testManager = new TopicManager()
                        testManager.setSubscribeCallback(async () => true)
                        testManager.setUnsubscribeCallback(async () => true)

                        // 订阅所有主题
                        for (const topic of topics) {
                            await testManager.subscribe(topic, 0, 'test')
                        }

                        // 取消订阅所有主题
                        for (const topic of topics) {
                            await testManager.unsubscribe(topic)
                        }

                        // 订阅列表应该为空
                        expect(testManager.getCount()).toBe(0)
                        expect(testManager.getSubscriptions()).toEqual([])
                    }
                ),
                { numRuns: 100 }
            )
        })

        it('查询返回的列表与实际订阅状态一致', () => {
            fc.assert(
                fc.asyncProperty(
                    fc.array(
                        fc.record({
                            topic: fc.string({ minLength: 1, maxLength: 50 }),
                            action: fc.constantFrom('subscribe', 'unsubscribe'),
                        }),
                        { minLength: 1, maxLength: 30 }
                    ),
                    async (operations) => {
                        const testManager = new TopicManager()
                        testManager.setSubscribeCallback(async () => true)
                        testManager.setUnsubscribeCallback(async () => true)

                        // 跟踪预期的订阅状态
                        const expectedSubscribed = new Set<string>()

                        // 执行操作
                        for (const op of operations) {
                            if (op.action === 'subscribe') {
                                await testManager.subscribe(op.topic, 0, 'test')
                                expectedSubscribed.add(op.topic)
                            } else {
                                await testManager.unsubscribe(op.topic)
                                expectedSubscribed.delete(op.topic)
                            }
                        }

                        // 验证一致性
                        const actualTopics = new Set(testManager.getTopics())
                        expect(actualTopics).toEqual(expectedSubscribed)

                        // 验证 isSubscribed 方法
                        for (const topic of expectedSubscribed) {
                            expect(testManager.isSubscribed(topic)).toBe(true)
                        }
                    }
                ),
                { numRuns: 100 }
            )
        })
    })
})
