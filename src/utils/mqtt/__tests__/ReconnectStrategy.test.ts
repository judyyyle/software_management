/**
 * ReconnectStrategy 测试
 * **Feature: mqtt-refactor, Property 1: 指数退避延迟计算正确性**
 * **Validates: Requirements 1.2**
 */
import { describe, it, expect, beforeEach } from 'vitest'
import fc from 'fast-check'
import { ReconnectStrategy } from '../ReconnectStrategy'

describe('ReconnectStrategy', () => {
    let strategy: ReconnectStrategy

    beforeEach(() => {
        strategy = new ReconnectStrategy()
    })

    describe('基础功能', () => {
        it('初始重连次数应该为0', () => {
            expect(strategy.getAttempts()).toBe(0)
        })

        it('前3次重连应该使用初始延迟（3秒）', () => {
            expect(strategy.getNextDelay()).toBe(3000) // 第1次
            expect(strategy.getNextDelay()).toBe(3000) // 第2次
            expect(strategy.getNextDelay()).toBe(3000) // 第3次
        })

        it('第4次开始应该使用指数退避（5秒、10秒、20秒...）', () => {
            // 消耗前3次
            strategy.getNextDelay()
            strategy.getNextDelay()
            strategy.getNextDelay()

            expect(strategy.getNextDelay()).toBe(5000)  // 第4次: 5秒
            expect(strategy.getNextDelay()).toBe(10000) // 第5次: 10秒
            expect(strategy.getNextDelay()).toBe(20000) // 第6次: 20秒
            expect(strategy.getNextDelay()).toBe(40000) // 第7次: 40秒
        })

        it('延迟不应超过最大延迟', () => {
            const customStrategy = new ReconnectStrategy({ maxDelay: 30000 })

            // 消耗前3次
            for (let i = 0; i < 3; i++) {
                customStrategy.getNextDelay()
            }

            // 继续获取延迟直到达到最大值
            for (let i = 0; i < 10; i++) {
                const delay = customStrategy.getNextDelay()
                expect(delay).toBeLessThanOrEqual(30000)
            }
        })

        it('reset 应该重置重连次数', () => {
            strategy.getNextDelay()
            strategy.getNextDelay()
            expect(strategy.getAttempts()).toBe(2)

            strategy.reset()
            expect(strategy.getAttempts()).toBe(0)
        })

        it('shouldRetry 在无限重试模式下应该始终返回 true', () => {
            const infiniteStrategy = new ReconnectStrategy({ maxAttempts: 0 })

            for (let i = 0; i < 100; i++) {
                infiniteStrategy.getNextDelay()
                expect(infiniteStrategy.shouldRetry()).toBe(true)
            }
        })

        it('shouldRetry 在有限重试模式下应该正确返回', () => {
            const limitedStrategy = new ReconnectStrategy({ maxAttempts: 5 })

            for (let i = 0; i < 5; i++) {
                expect(limitedStrategy.shouldRetry()).toBe(true)
                limitedStrategy.getNextDelay()
            }

            expect(limitedStrategy.shouldRetry()).toBe(false)
        })
    })

    describe('Property 1: 指数退避延迟计算正确性', () => {
        /**
         * **Feature: mqtt-refactor, Property 1: 指数退避延迟计算正确性**
         * **Validates: Requirements 1.2**
         *
         * 对于任意重连尝试次数 n（n >= 4），重连延迟应该等于
         * min(5000 * 2^(n-4), maxDelay)
         */
        it('指数退避延迟应该符合公式计算', () => {
            fc.assert(
                fc.property(
                    fc.integer({ min: 4, max: 20 }), // 重连次数（从第4次开始）
                    (attempts) => {
                        const testStrategy = new ReconnectStrategy({
                            initialDelay: 3000,
                            maxDelay: 60000,
                            backoffMultiplier: 2,
                        })

                        // 模拟前 attempts 次重连
                        let lastDelay = 0
                        for (let i = 0; i < attempts; i++) {
                            lastDelay = testStrategy.getNextDelay()
                        }

                        // 计算期望的延迟
                        let expectedDelay: number
                        if (attempts <= 3) {
                            expectedDelay = 3000
                        } else {
                            expectedDelay = Math.min(5000 * Math.pow(2, attempts - 4), 60000)
                        }

                        expect(lastDelay).toBe(expectedDelay)
                    }
                ),
                { numRuns: 100 }
            )
        })

        it('延迟应该始终为正数', () => {
            fc.assert(
                fc.property(fc.integer({ min: 1, max: 50 }), (attempts) => {
                    const testStrategy = new ReconnectStrategy()

                    for (let i = 0; i < attempts; i++) {
                        const delay = testStrategy.getNextDelay()
                        expect(delay).toBeGreaterThan(0)
                    }
                }),
                { numRuns: 100 }
            )
        })

        it('延迟应该始终不超过最大延迟', () => {
            fc.assert(
                fc.property(
                    fc.integer({ min: 1, max: 100 }),
                    fc.integer({ min: 10000, max: 120000 }), // maxDelay 必须大于 initialDelay (3000) 和 baseDelay (5000)
                    (attempts, maxDelay) => {
                        const testStrategy = new ReconnectStrategy({ maxDelay })

                        for (let i = 0; i < attempts; i++) {
                            const delay = testStrategy.getNextDelay()
                            expect(delay).toBeLessThanOrEqual(maxDelay)
                        }
                    }
                ),
                { numRuns: 100 }
            )
        })

        it('重置后延迟应该回到初始值', () => {
            fc.assert(
                fc.property(fc.integer({ min: 1, max: 20 }), (attempts) => {
                    const testStrategy = new ReconnectStrategy()

                    // 执行多次重连
                    for (let i = 0; i < attempts; i++) {
                        testStrategy.getNextDelay()
                    }

                    // 重置
                    testStrategy.reset()

                    // 第一次延迟应该是初始延迟
                    expect(testStrategy.getNextDelay()).toBe(3000)
                }),
                { numRuns: 100 }
            )
        })
    })
})
