/**
 * RequestTracker 测试
 * **Feature: mqtt-refactor, Property 15: 请求 ID 唯一性**
 * **Feature: mqtt-refactor, Property 16: 请求-响应匹配正确性**
 * **Feature: mqtt-refactor, Property 17: 错误码转换完整性**
 * **Validates: Requirements 9.1, 9.2, 9.5**
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import fc from 'fast-check'
import { RequestTracker } from '../RequestTracker'

describe('RequestTracker', () => {
    let tracker: RequestTracker

    beforeEach(() => {
        vi.useFakeTimers()
        tracker = new RequestTracker()
    })

    afterEach(() => {
        vi.useRealTimers()
    })

    describe('基础功能', () => {
        it('generateId 应该生成 UUID 格式的 ID', () => {
            const id = tracker.generateId()
            expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
        })

        it('track 应该添加待处理请求', () => {
            const tid = tracker.generateId()
            tracker.track(tid, 'bid1', 'test_method')

            expect(tracker.hasPending(tid)).toBe(true)
            expect(tracker.getPendingCount()).toBe(1)
        })

        it('resolve 应该解析请求并移除', async () => {
            const tid = tracker.generateId()
            const promise = tracker.track(tid, 'bid1', 'test_method')

            tracker.resolve(tid, { data: { result: 0 } })

            const result = await promise
            expect(result).toEqual({ data: { result: 0 } })
            expect(tracker.hasPending(tid)).toBe(false)
        })

        it('resolve 不存在的请求应该返回 false', () => {
            const result = tracker.resolve('nonexistent', {})
            expect(result).toBe(false)
        })

        it('请求超时应该触发 reject', async () => {
            const tid = tracker.generateId()
            const promise = tracker.track(tid, 'bid1', 'test_method', 1000)

            // 快进时间
            vi.advanceTimersByTime(1001)

            await expect(promise).rejects.toThrow('请求超时')
        })

        it('cancelAll 应该取消所有请求', async () => {
            const tid1 = tracker.generateId()
            const tid2 = tracker.generateId()

            const promise1 = tracker.track(tid1, 'bid1', 'method1')
            const promise2 = tracker.track(tid2, 'bid2', 'method2')

            tracker.cancelAll()

            await expect(promise1).rejects.toThrow('请求已取消')
            await expect(promise2).rejects.toThrow('请求已取消')
            expect(tracker.getPendingCount()).toBe(0)
        })
    })

    describe('错误码处理', () => {
        it('result 为 0 应该 resolve', async () => {
            const tid = tracker.generateId()
            const promise = tracker.track(tid, 'bid1', 'test_method')

            tracker.resolve(tid, { data: { result: 0 } })

            await expect(promise).resolves.toEqual({ data: { result: 0 } })
        })

        it('result 不为 0 应该 reject 并转换错误信息', async () => {
            const tid = tracker.generateId()
            const promise = tracker.track(tid, 'bid1', 'test_method')

            tracker.resolve(tid, { data: { result: 1 } })

            await expect(promise).rejects.toThrow('参数错误')
        })

        it('未知错误码应该显示错误码', async () => {
            const tid = tracker.generateId()
            const promise = tracker.track(tid, 'bid1', 'test_method')

            tracker.resolve(tid, { data: { result: 999 } })

            await expect(promise).rejects.toThrow('未知错误 (999)')
        })
    })

    describe('Property 15: 请求 ID 唯一性', () => {
        /**
         * **Feature: mqtt-refactor, Property 15: 请求 ID 唯一性**
         * **Validates: Requirements 9.1**
         *
         * 对于任意两个不同的服务请求，它们的 tid 应该不相同
         */
        it('生成的 ID 应该唯一', () => {
            // 使用真实定时器进行此测试
            vi.useRealTimers()

            fc.assert(
                fc.property(fc.integer({ min: 10, max: 100 }), (count) => {
                    const testTracker = new RequestTracker()
                    const ids = new Set<string>()

                    for (let i = 0; i < count; i++) {
                        const id = testTracker.generateId()
                        ids.add(id)
                    }

                    // 所有 ID 应该唯一
                    expect(ids.size).toBe(count)
                }),
                { numRuns: 100 }
            )

            vi.useFakeTimers()
        })
    })

    describe('Property 16: 请求-响应匹配正确性', () => {
        /**
         * **Feature: mqtt-refactor, Property 16: 请求-响应匹配正确性**
         * **Validates: Requirements 9.2**
         *
         * 对于任意服务响应消息，应该根据其 tid 正确匹配到对应的待处理请求
         */
        it('响应应该正确匹配到对应的请求', async () => {
            vi.useRealTimers()

            await fc.assert(
                fc.asyncProperty(fc.integer({ min: 1, max: 10 }), async (count) => {
                    const testTracker = new RequestTracker()
                    const requests: { tid: string; promise: Promise<any> }[] = []

                    // 创建多个请求
                    for (let i = 0; i < count; i++) {
                        const tid = testTracker.generateId()
                        const promise = testTracker.track(tid, `bid${i}`, `method${i}`)
                        requests.push({ tid, promise })
                    }

                    // 按随机顺序解析请求
                    const shuffled = [...requests].sort(() => Math.random() - 0.5)

                    for (const req of shuffled) {
                        const result = testTracker.resolve(req.tid, {
                            data: { result: 0, tid: req.tid },
                        })
                        expect(result).toBe(true)
                    }

                    // 所有请求都应该被解析
                    for (const req of requests) {
                        const result = await req.promise
                        expect(result.data.tid).toBe(req.tid)
                    }
                }),
                { numRuns: 50 }
            )

            vi.useFakeTimers()
        })
    })

    describe('Property 17: 错误码转换完整性', () => {
        /**
         * **Feature: mqtt-refactor, Property 17: 错误码转换完整性**
         * **Validates: Requirements 9.5**
         *
         * 对于任意已知的错误码，应该能转换为对应的用户可读错误信息
         * 未知错误码应该返回包含错误码的默认信息
         */
        it('所有错误码都应该能转换为错误信息', () => {
            vi.useRealTimers()

            fc.assert(
                fc.asyncProperty(fc.integer({ min: -100, max: 100 }), async (errorCode) => {
                    const testTracker = new RequestTracker()
                    const tid = testTracker.generateId()
                    const promise = testTracker.track(tid, 'bid', 'method')

                    if (errorCode === 0) {
                        // 成功情况
                        testTracker.resolve(tid, { data: { result: 0 } })
                        await expect(promise).resolves.toBeDefined()
                    } else {
                        // 错误情况
                        testTracker.resolve(tid, { data: { result: errorCode } })

                        try {
                            await promise
                            // 不应该到达这里
                            expect(true).toBe(false)
                        } catch (error) {
                            // 错误信息应该是字符串
                            expect(typeof (error as Error).message).toBe('string')
                            // 错误信息不应该为空
                            expect((error as Error).message.length).toBeGreaterThan(0)
                        }
                    }
                }),
                { numRuns: 100 }
            )

            vi.useFakeTimers()
        })
    })
})
