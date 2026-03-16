import type { App, Directive, DirectiveBinding } from 'vue'
import { useLicenseStore } from '/@/stores/license'

/**
 * v-license 指令
 * 用于控制元素的显示/隐藏或禁用状态
 * 
 * 使用方式:
 * - v-license="'featureId'" - 无权限时隐藏元素
 * - v-license:disable="'featureId'" - 无权限时禁用元素
 * - v-license:hide="'featureId'" - 无权限时隐藏元素（同默认）
 * - v-license:remove="'featureId'" - 无权限时移除元素
 */
export const licenseDirective: Directive = {
    mounted(el: HTMLElement, binding: DirectiveBinding) {
        checkLicense(el, binding)
    },
    updated(el: HTMLElement, binding: DirectiveBinding) {
        checkLicense(el, binding)
    },
}

function checkLicense(el: HTMLElement, binding: DirectiveBinding) {
    const licenseStore = useLicenseStore()
    const featureId = binding.value
    const mode = binding.arg || 'hide' // hide | disable | remove

    if (!featureId) return

    const hasPermission = licenseStore.hasFeature(featureId)

    if (!hasPermission) {
        switch (mode) {
            case 'disable':
                el.setAttribute('disabled', 'disabled')
                el.classList.add('is-disabled', 'license-disabled')
                el.style.pointerEvents = 'none'
                el.style.opacity = '0.5'
                break
            case 'remove':
                el.parentNode?.removeChild(el)
                break
            case 'hide':
            default:
                el.style.display = 'none'
                break
        }
    } else {
        // 恢复元素状态
        el.removeAttribute('disabled')
        el.classList.remove('is-disabled', 'license-disabled')
        el.style.pointerEvents = ''
        el.style.opacity = ''
        el.style.display = ''
    }
}

/**
 * v-license-module 指令
 * 用于检查模块级别的授权
 */
export const licenseModuleDirective: Directive = {
    mounted(el: HTMLElement, binding: DirectiveBinding) {
        checkModuleLicense(el, binding)
    },
    updated(el: HTMLElement, binding: DirectiveBinding) {
        checkModuleLicense(el, binding)
    },
}

function checkModuleLicense(el: HTMLElement, binding: DirectiveBinding) {
    const licenseStore = useLicenseStore()
    const moduleId = binding.value
    const mode = binding.arg || 'hide'

    if (!moduleId) return

    const hasPermission = licenseStore.hasModule(moduleId)

    if (!hasPermission) {
        switch (mode) {
            case 'disable':
                el.setAttribute('disabled', 'disabled')
                el.classList.add('is-disabled', 'license-disabled')
                el.style.pointerEvents = 'none'
                el.style.opacity = '0.5'
                break
            case 'remove':
                el.parentNode?.removeChild(el)
                break
            case 'hide':
            default:
                el.style.display = 'none'
                break
        }
    } else {
        el.removeAttribute('disabled')
        el.classList.remove('is-disabled', 'license-disabled')
        el.style.pointerEvents = ''
        el.style.opacity = ''
        el.style.display = ''
    }
}

/**
 * 注册授权相关指令
 */
export function setupLicenseDirectives(app: App) {
    app.directive('license', licenseDirective)
    app.directive('license-module', licenseModuleDirective)
}

/**
 * 授权检查工具函数
 */
export function useLicense() {
    const licenseStore = useLicenseStore()

    return {
        /**
         * 检查功能点是否授权
         */
        hasFeature: (featureId: string) => licenseStore.hasFeature(featureId),

        /**
         * 检查模块是否授权
         */
        hasModule: (moduleId: string) => licenseStore.hasModule(moduleId),

        /**
         * 检查限制
         */
        checkLimit: (type: 'devices' | 'projects' | 'users', current: number) => 
            licenseStore.checkLimit(type, current),

        /**
         * 获取授权状态
         */
        isValid: () => licenseStore.isValid,

        /**
         * 获取剩余天数
         */
        remainingDays: () => licenseStore.remainingDays,

        /**
         * 是否即将到期
         */
        isExpiringSoon: () => licenseStore.isExpiringSoon,

        /**
         * 是否已过期
         */
        isExpired: () => licenseStore.isExpired,
    }
}
