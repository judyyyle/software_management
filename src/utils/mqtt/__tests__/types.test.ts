/**
 * MQTT 类型定义测试
 * 验证类型定义和工具函数
 */
import { describe, it, expect } from 'vitest'
import {
    TopicPatterns,
    getErrorMessage,
    ErrorCodeMap,
    DEFAULT_CONNECTION_CONFIG,
    DEFAULT_RECONNECT_CONFIG,
    DEFAULT_HEARTBEAT_CONFIG,
} from '../types'

describe('TopicPatterns', () => {
    it('应该生成正确的设备 OSD 主题', () => {
        const sn = 'TEST123'
        expect(TopicPatterns.DEVICE_OSD(sn)).toBe('thing/product/TEST123/osd')
    })

    it('应该生成正确的服务主题', () => {
        const sn = 'TEST123'
        expect(TopicPatterns.SERVICES(sn)).toBe('thing/product/TEST123/services')
        expect(TopicPatterns.SERVICES_REPLY(sn)).toBe('thing/product/TEST123/services_reply')
    })

    it('应该生成正确的 DRC 主题', () => {
        const sn = 'TEST123'
        expect(TopicPatterns.DRC_UP(sn)).toBe('thing/product/TEST123/drc/up')
        expect(TopicPatterns.DRC_DOWN(sn)).toBe('thing/product/TEST123/drc/down')
    })
})

describe('getErrorMessage', () => {
    it('应该返回已知错误码的错误信息', () => {
        expect(getErrorMessage(0)).toBe('成功')
        expect(getErrorMessage(1)).toBe('参数错误')
        expect(getErrorMessage(2)).toBe('设备忙')
    })

    it('应该返回未知错误码的默认信息', () => {
        expect(getErrorMessage(999)).toBe('未知错误 (999)')
        expect(getErrorMessage(-1)).toBe('未知错误 (-1)')
    })
})

describe('默认配置', () => {
    it('DEFAULT_CONNECTION_CONFIG 应该有正确的默认值', () => {
        expect(DEFAULT_CONNECTION_CONFIG.keepalive).toBe(60)
        expect(DEFAULT_CONNECTION_CONFIG.connectTimeout).toBe(30000)
    })

    it('DEFAULT_RECONNECT_CONFIG 应该有正确的默认值', () => {
        expect(DEFAULT_RECONNECT_CONFIG.initialDelay).toBe(3000)
        expect(DEFAULT_RECONNECT_CONFIG.maxDelay).toBe(60000)
        expect(DEFAULT_RECONNECT_CONFIG.backoffMultiplier).toBe(2)
    })

    it('DEFAULT_HEARTBEAT_CONFIG 应该有正确的默认值', () => {
        expect(DEFAULT_HEARTBEAT_CONFIG.interval).toBe(30000)
        expect(DEFAULT_HEARTBEAT_CONFIG.timeout).toBe(60000)
    })
})
