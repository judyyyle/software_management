/**
 * MessageRouter 测试
 * **Feature: mqtt-refactor, Property 7: 消息分发完整性**
 * **Feature: mqtt-refactor, Property 8: 主题通配符匹配正确性**
 * **Feature: mqtt-refactor, Property 9: 消息处理器错误隔离**
 * **Validates: Requirements 5.1, 5.2, 5.3**
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import fc from 'fast-check'
import { MessageRouter } from '../MessageRouter'

describe('MessageRouter', () => {
    let router: MessageRouter

    beforeEach(() => {
        router = new MessageRouter()
    })

    describe('基础功能', () => {
        it('初始处理器数量应该为0', () => {
            expect(router.getCount()).toBe(0)
        })

        it('register 应该添加处理器并返回 id', () => {
            const handler = vi.fn()
            const id = router.register('test/topic', handler)

            expect(id).toBeDefined()
            expect(router.getCount()).toBe(1)
            expect(router.hasHandler(id)).toBe(true)
        })

        it('unregister 应该移除处理器', () => {
            const handler = vi.fn()
            const id = router.register('test/topic', handler)

            const result = router.unregister(id)

            expect(result).toBe(true)
            expect(router.getCount()).toBe(0)
        })

        it('route 应该调用匹配的处理器', () => {
            const handler = vi.fn()
            router.register('test/topic', handler)

            router.route('test/topic', Buffer.from('{"data": "test"}'))

            expect(handler).toHaveBeenCalledTimes(1)
            expect(handler).toHaveBeenCalledWith('test/topic', { data: 'test' })
        })

        it('route 不应该调用不匹配的处理器', () => {
            const handler = vi.fn()
            router.register('test/topic', handler)

            router.route('other/topic', Buffer.from('{}'))

            expect(handler).not.toHaveBeenCalled()
        })
    })

    describe('通配符匹配', () => {
        it('# 应该匹配所有主题', () => {
            const handler = vi.fn()
            router.register('#', handler)

            router.route('any/topic/here', Buffer.from('{}'))
            router.route('another', Buffer.from('{}'))

            expect(handler).toHaveBeenCalledTimes(2)
        })

        it('+ 应该匹配单个层级', () => {
            const handler = vi.fn()
            router.register('test/+/data', handler)

            router.route('test/device1/data', Buffer.from('{}'))
            router.route('test/device2/data', Buffer.from('{}'))

            expect(handler).toHaveBeenCalledTimes(2)
        })

        it('+ 不应该匹配多个层级', () => {
            const handler = vi.fn()
            router.register('test/+/data', handler)

            router.route('test/device1/extra/data', Buffer.from('{}'))

            expect(handler).not.toHaveBeenCalled()
        })

        it('thing/product/+/osd 应该匹配设备 OSD 主题', () => {
            const handler = vi.fn()
            router.register('thing/product/+/osd', handler)

            router.route('thing/product/DEVICE123/osd', Buffer.from('{}'))
            router.route('thing/product/DEVICE456/osd', Buffer.from('{}'))

            expect(handler).toHaveBeenCalledTimes(2)
        })

        it('# 在中间应该匹配剩余所有层级', () => {
            const handler = vi.fn()
            router.register('test/#', handler)

            router.route('test/a', Buffer.from('{}'))
            router.route('test/a/b', Buffer.from('{}'))
            router.route('test/a/b/c', Buffer.from('{}'))

            expect(handler).toHaveBeenCalledTimes(3)
        })
    })

    describe('错误隔离', () => {
        it('一个处理器抛出异常不应该影响其他处理器', () => {
            const errorHandler = vi.fn(() => {
                throw new Error('Test error')
            })
            const normalHandler = vi.fn()

            router.register('#', errorHandler)
            router.register('#', normalHandler)

            // 不应该抛出异常
            expect(() => {
                router.route('test/topic', Buffer.from('{}'))
            }).not.toThrow()

            // 两个处理器都应该被调用
            expect(errorHandler).toHaveBeenCalledTimes(1)
            expect(normalHandler).toHaveBeenCalledTimes(1)
        })
    })

    describe('Property 7: 消息分发完整性', () => {
        /**
         * **Feature: mqtt-refactor, Property 7: 消息分发完整性**
         * **Validates: Requirements 5.1**
         *
         * 对于任意消息和任意数量的已注册消息处理器，
         * 所有主题模式匹配的处理器都应该收到该消息
         */
        it('所有匹配的处理器都应该收到消息', () => {
            fc.assert(
                fc.property(
                    fc.integer({ min: 1, max: 10 }), // 处理器数量
                    fc.string({ minLength: 1, maxLength: 20 }), // 主题
                    (handlerCount, topic) => {
                        const testRouter = new MessageRouter()
                        const handlers: ReturnType<typeof vi.fn>[] = []

                        // 注册多个处理器，都使用 # 匹配所有
                        for (let i = 0; i < handlerCount; i++) {
                            const handler = vi.fn()
                            handlers.push(handler)
                            testRouter.register('#', handler)
                        }

                        // 发送消息
                        testRouter.route(topic, Buffer.from('{}'))

                        // 所有处理器都应该被调用
                        for (const handler of handlers) {
                            expect(handler).toHaveBeenCalledTimes(1)
                        }
                    }
                ),
                { numRuns: 100 }
            )
        })
    })

    describe('Property 8: 主题通配符匹配正确性', () => {
        /**
         * **Feature: mqtt-refactor, Property 8: 主题通配符匹配正确性**
         * **Validates: Requirements 5.2**
         */
        it('精确匹配应该正确工作', () => {
            fc.assert(
                fc.property(
                    fc.array(fc.string({ minLength: 1, maxLength: 10 }), { minLength: 1, maxLength: 5 }),
                    (parts) => {
                        const topic = parts.join('/')
                        expect(router.topicMatch(topic, topic)).toBe(true)
                    }
                ),
                { numRuns: 100 }
            )
        })

        it('# 应该匹配任意主题', () => {
            fc.assert(
                fc.property(
                    fc.array(fc.string({ minLength: 1, maxLength: 10 }), { minLength: 1, maxLength: 5 }),
                    (parts) => {
                        const topic = parts.join('/')
                        expect(router.topicMatch('#', topic)).toBe(true)
                    }
                ),
                { numRuns: 100 }
            )
        })

        it('+ 应该匹配单个层级', () => {
            fc.assert(
                fc.property(
                    fc.stringMatching(/^[a-zA-Z0-9]+$/), // 不包含 / 的字符串
                    fc.stringMatching(/^[a-zA-Z0-9]+$/),
                    fc.stringMatching(/^[a-zA-Z0-9]+$/),
                    (prefix, middle, suffix) => {
                        if (!prefix || !middle || !suffix) return true // 跳过空字符串
                        const pattern = `${prefix}/+/${suffix}`
                        const topic = `${prefix}/${middle}/${suffix}`
                        expect(router.topicMatch(pattern, topic)).toBe(true)
                    }
                ),
                { numRuns: 100 }
            )
        })
    })

    describe('Property 9: 消息处理器错误隔离', () => {
        /**
         * **Feature: mqtt-refactor, Property 9: 消息处理器错误隔离**
         * **Validates: Requirements 5.3**
         *
         * 如果某个处理器抛出异常，其他处理器仍然应该正常执行
         */
        it('异常处理器不应该影响其他处理器', () => {
            fc.assert(
                fc.property(
                    fc.integer({ min: 1, max: 5 }), // 正常处理器数量
                    fc.integer({ min: 1, max: 3 }), // 异常处理器数量
                    (normalCount, errorCount) => {
                        const testRouter = new MessageRouter()
                        const normalHandlers: ReturnType<typeof vi.fn>[] = []
                        const errorHandlers: ReturnType<typeof vi.fn>[] = []

                        // 注册正常处理器
                        for (let i = 0; i < normalCount; i++) {
                            const handler = vi.fn()
                            normalHandlers.push(handler)
                            testRouter.register('#', handler)
                        }

                        // 注册异常处理器
                        for (let i = 0; i < errorCount; i++) {
                            const handler = vi.fn(() => {
                                throw new Error('Test error')
                            })
                            errorHandlers.push(handler)
                            testRouter.register('#', handler)
                        }

                        // 发送消息不应该抛出异常
                        expect(() => {
                            testRouter.route('test/topic', Buffer.from('{}'))
                        }).not.toThrow()

                        // 所有处理器都应该被调用
                        for (const handler of normalHandlers) {
                            expect(handler).toHaveBeenCalledTimes(1)
                        }
                        for (const handler of errorHandlers) {
                            expect(handler).toHaveBeenCalledTimes(1)
                        }
                    }
                ),
                { numRuns: 100 }
            )
        })
    })
})
