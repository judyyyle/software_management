import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { licenseApi } from '/@/api/frontend/license'
import { Local } from '/@/utils/storage'

const LICENSE_CACHE_KEY = 'license_data'
const LICENSE_KEY_STORAGE = 'license_key'
const FINGERPRINT_STORAGE = 'hardware_fingerprint'

export interface LicenseData {
    license_key: string
    modules: string[]
    features: string[]
    limits: {
        max_devices?: number
        max_projects?: number
        max_users?: number
        used_devices?: number
        used_projects?: number
        used_users?: number
    }
    expires_at: string
    remaining_days: number
    offline_mode: boolean
    max_offline_days: number
}

export const useLicenseStore = defineStore('license', () => {
    // 状态
    const licenseData = ref<LicenseData | null>(null)
    const isActivated = ref(false)
    const isValid = ref(false)
    const lastVerifyTime = ref<number>(0)
    const offlineDays = ref(0)

    // 计算属性
    const licenseKey = computed(() => Local.get(LICENSE_KEY_STORAGE) || '')
    const fingerprint = computed(() => Local.get(FINGERPRINT_STORAGE) || '')
    
    const modules = computed(() => licenseData.value?.modules || [])
    const features = computed(() => licenseData.value?.features || [])
    const limits = computed(() => licenseData.value?.limits || {})
    const remainingDays = computed(() => licenseData.value?.remaining_days || 0)
    
    const isExpiringSoon = computed(() => remainingDays.value > 0 && remainingDays.value <= 7)
    const isExpired = computed(() => remainingDays.value <= 0)

    // 检查模块是否授权
    const hasModule = (moduleId: string): boolean => {
        return modules.value.includes(moduleId)
    }

    // 检查功能点是否授权
    const hasFeature = (featureId: string): boolean => {
        return features.value.includes(featureId)
    }

    // 检查限制
    const checkLimit = (type: 'devices' | 'projects' | 'users', current: number): boolean => {
        const max = limits.value[`max_${type}` as keyof typeof limits.value] as number
        if (!max) return true // 无限制
        return current < max
    }

    // 生成硬件指纹
    const generateFingerprint = async (): Promise<string> => {
        // 在浏览器环境中，使用可用的信息生成指纹
        const canvas = document.createElement('canvas')
        const ctx = canvas.getContext('2d')
        if (ctx) {
            ctx.textBaseline = 'top'
            ctx.font = '14px Arial'
            ctx.fillText('fingerprint', 2, 2)
        }
        
        const components = [
            navigator.userAgent,
            navigator.language,
            screen.width + 'x' + screen.height,
            new Date().getTimezoneOffset(),
            canvas.toDataURL(),
        ]
        
        const str = components.join('|')
        const encoder = new TextEncoder()
        const data = encoder.encode(str)
        const hashBuffer = await crypto.subtle.digest('SHA-256', data)
        const hashArray = Array.from(new Uint8Array(hashBuffer))
        const hashHex = hashArray.map(b => b.toString(16).padStart(2, '0')).join('')
        
        return hashHex
    }

    // 初始化
    const initialize = async () => {
        // 尝试从缓存加载
        const cached = Local.get(LICENSE_CACHE_KEY)
        if (cached) {
            licenseData.value = cached.data
            lastVerifyTime.value = cached.time
            isActivated.value = true
            isValid.value = true
        }

        // 生成指纹
        if (!fingerprint.value) {
            const fp = await generateFingerprint()
            Local.set(FINGERPRINT_STORAGE, fp)
        }
    }

    // 激活授权
    const activate = async (key: string): Promise<{ success: boolean; message: string }> => {
        try {
            const fp = fingerprint.value || await generateFingerprint()
            if (!fingerprint.value) {
                Local.set(FINGERPRINT_STORAGE, fp)
            }

            const res = await licenseApi.activate(key, fp, {
                userAgent: navigator.userAgent,
                platform: navigator.platform,
                language: navigator.language,
            })

            if (res.code === 1) {
                Local.set(LICENSE_KEY_STORAGE, key)
                licenseData.value = res.data
                isActivated.value = true
                isValid.value = true
                lastVerifyTime.value = Date.now()
                
                // 缓存授权数据
                Local.set(LICENSE_CACHE_KEY, {
                    data: res.data,
                    time: Date.now(),
                })

                return { success: true, message: res.msg || '激活成功' }
            }

            return { success: false, message: res.msg || '激活失败' }
        } catch (error: any) {
            return { success: false, message: error.message || '激活失败' }
        }
    }

    // 验证授权
    const verify = async (featureId?: string): Promise<boolean> => {
        if (!licenseKey.value || !fingerprint.value) {
            isValid.value = false
            return false
        }

        try {
            const res = await licenseApi.verify(licenseKey.value, fingerprint.value, featureId)
            
            if (res.code === 1) {
                licenseData.value = res.data
                isValid.value = true
                lastVerifyTime.value = Date.now()
                offlineDays.value = 0
                
                // 更新缓存
                Local.set(LICENSE_CACHE_KEY, {
                    data: res.data,
                    time: Date.now(),
                })

                return true
            }

            isValid.value = false
            return false
        } catch (error) {
            // 网络错误时检查离线模式
            if (licenseData.value?.offline_mode) {
                const maxOfflineDays = licenseData.value.max_offline_days || 7
                const lastVerify = lastVerifyTime.value
                const daysSinceLastVerify = Math.floor((Date.now() - lastVerify) / (24 * 60 * 60 * 1000))
                
                if (daysSinceLastVerify <= maxOfflineDays) {
                    offlineDays.value = daysSinceLastVerify
                    return true
                }
            }
            
            isValid.value = false
            return false
        }
    }

    // 心跳检测
    const heartbeat = async (): Promise<boolean> => {
        if (!licenseKey.value || !fingerprint.value) {
            return false
        }

        try {
            const res = await licenseApi.heartbeat(licenseKey.value, fingerprint.value)
            
            if (res.code === 1) {
                if (licenseData.value) {
                    licenseData.value.remaining_days = res.data.remaining_days
                }
                lastVerifyTime.value = Date.now()
                offlineDays.value = 0
                return true
            }

            return false
        } catch (error) {
            return false
        }
    }

    // 检查功能点授权（带缓存）
    const checkFeature = async (featureId: string): Promise<boolean> => {
        // 先检查本地缓存
        if (hasFeature(featureId)) {
            return true
        }

        // 如果本地没有，尝试在线验证
        const cacheAge = Date.now() - lastVerifyTime.value
        if (cacheAge > 60000) { // 缓存超过1分钟，重新验证
            await verify(featureId)
        }

        return hasFeature(featureId)
    }

    // 批量检查功能点
    const checkFeatures = async (featureIds: string[]): Promise<Record<string, boolean>> => {
        if (!licenseKey.value || !fingerprint.value) {
            return featureIds.reduce((acc, id) => ({ ...acc, [id]: false }), {})
        }

        try {
            const res = await licenseApi.checkFeatures(licenseKey.value, fingerprint.value, featureIds)
            
            if (res.code === 1) {
                return res.data.features
            }
        } catch (error) {
            // 使用本地缓存
        }

        return featureIds.reduce((acc, id) => ({ ...acc, [id]: hasFeature(id) }), {})
    }

    // 清除授权
    const clear = () => {
        licenseData.value = null
        isActivated.value = false
        isValid.value = false
        lastVerifyTime.value = 0
        offlineDays.value = 0
        Local.remove(LICENSE_CACHE_KEY)
        Local.remove(LICENSE_KEY_STORAGE)
    }

    // 启动心跳定时器
    let heartbeatTimer: number | null = null
    
    const startHeartbeat = (interval = 5 * 60 * 1000) => {
        stopHeartbeat()
        heartbeatTimer = window.setInterval(() => {
            heartbeat()
        }, interval)
    }

    const stopHeartbeat = () => {
        if (heartbeatTimer) {
            clearInterval(heartbeatTimer)
            heartbeatTimer = null
        }
    }

    return {
        // 状态
        licenseData,
        isActivated,
        isValid,
        lastVerifyTime,
        offlineDays,
        
        // 计算属性
        licenseKey,
        fingerprint,
        modules,
        features,
        limits,
        remainingDays,
        isExpiringSoon,
        isExpired,
        
        // 方法
        hasModule,
        hasFeature,
        checkLimit,
        initialize,
        activate,
        verify,
        heartbeat,
        checkFeature,
        checkFeatures,
        clear,
        startHeartbeat,
        stopHeartbeat,
    }
})
