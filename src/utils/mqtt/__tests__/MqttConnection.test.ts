/**
 * MqttConnection 单元测试
 * Requirements: 1.1, 1.2, 1.3
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { MqttConnection } from '../MqttConnection'
import { ConnectionState } from '../types'

// Mock mqtt 模块
vi.mock('mqtt', () => {
    const mockClient = {
        connected: false,
        on: vi.fn(),
        end: vi.fn(),
        subscribe: vi.fn((topic, opts, cb) => cb && cb(null)),
        unsubscribe: vi.fn((topic, cb) => cb && cb(null)),
        publish: vi.fn((topic, msg, opts, cb) => cb && cb(null)),
    }

    return {
        default: {
            connect: vi.fn(() => mockClient),
        },
    }
})

describe('MqttConnection', () => {
    let connection: MqttConnection

    beforeEach(() => {
        vi.clearAllMocks()
        connection = new MqttConnection({
            host: 'test.mqtt.server',
            port: 8083,
        })
    })

    describe('初始化', () => {
        it('初始状态应该是 disconnected', () => {
            const state = connection.getState()
            expect(state.status).toBe('disconnected')
            expect(state.reconnectAttempts).toBe(0)
        })

        it('应该正确合并配置', () => {
            const config = connection.getConfig()
            expect(config.host).toBe('test.mqtt.server')
            expect(config.port).toBe(8083)
            expect(config.keepalive).toBe(60) // 默认值
        })
    })

    describe('状态管理', () => {
        it('onStateChange 应该注册回调', () => {
            const callback = vi.fn()
            const unsubscribe = connection.onStateChange(callback)

            expect(typeof unsubscribe).toBe('function')
        })

        it('onStateChange 返回的函数应该能取消注册', () => {
            const callback = vi.fn()
            const unsubscribe = connection.onStateChange(callback)

            unsubscribe()
            // 回调应该被移除（无法直接验证，但不应该抛出错误）
        })
    })

    describe('配置更新', () => {
        it('updateConfig 应该更新配置', async () => {
            await connection.updateConfig({ keepalive: 120 })

            const config = connection.getConfig()
            expect(config.keepalive).toBe(120)
        })

        it('updateConfig 应该拒绝空 host', async () => {
            const result = await connection.updateConfig({ host: '' })
            expect(result).toBe(false)
        })
    })

    describe('订阅管理', () => {
        it('getSubscribedTopics 初始应该返回空数组', () => {
            const topics = connection.getSubscribedTopics()
            expect(topics).toEqual([])
        })
    })

    describe('连接状态', () => {
        it('isConnected 初始应该返回 false', () => {
            expect(connection.isConnected()).toBe(false)
        })

        it('disconnect 应该更新状态为 disconnected', () => {
            connection.disconnect()

            const state = connection.getState()
            expect(state.status).toBe('disconnected')
        })
    })

    describe('心跳管理器', () => {
        it('应该能获取心跳管理器', () => {
            const heartbeatManager = connection.getHeartbeatManager()
            expect(heartbeatManager).toBeDefined()
            expect(typeof heartbeatManager.start).toBe('function')
            expect(typeof heartbeatManager.stop).toBe('function')
        })
    })
})

describe('MqttConnection 状态变化', () => {
    it('状态变化应该通知所有回调', () => {
        const connection = new MqttConnection({ host: 'test.mqtt.server' })
        const states: ConnectionState[] = []

        connection.onStateChange((state) => {
            states.push({ ...state })
        })

        // 触发断开连接（会更新状态）
        connection.disconnect()

        expect(states.length).toBeGreaterThan(0)
        expect(states[states.length - 1].status).toBe('disconnected')
    })
})
