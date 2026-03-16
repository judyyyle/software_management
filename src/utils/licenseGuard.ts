import type { Router, RouteLocationNormalized } from 'vue-router'
import { useLicenseStore } from '/@/stores/license'
import { ElMessage } from 'element-plus'

/**
 * 路由元信息中的授权配置
 */
export interface LicenseRouteMeta {
    /** 需要的功能点ID */
    licenseFeature?: string
    /** 需要的模块ID */
    licenseModule?: string
    /** 无权限时的跳转路径 */
    licenseRedirect?: string
}

/**
 * 设置授权路由守卫
 */
export function setupLicenseGuard(router: Router) {
    router.beforeEach(async (to, from, next) => {
        const licenseStore = useLicenseStore()
        
        // 初始化授权信息
        if (!licenseStore.isActivated) {
            await licenseStore.initialize()
        }

        // 检查路由是否需要授权
        const meta = to.meta as LicenseRouteMeta
        
        if (meta.licenseFeature) {
            if (!licenseStore.hasFeature(meta.licenseFeature)) {
                ElMessage.warning('该功能需要授权后使用')
                if (meta.licenseRedirect) {
                    return next(meta.licenseRedirect)
                }
                return next(false)
            }
        }

        if (meta.licenseModule) {
            if (!licenseStore.hasModule(meta.licenseModule)) {
                ElMessage.warning('该模块需要授权后使用')
                if (meta.licenseRedirect) {
                    return next(meta.licenseRedirect)
                }
                return next(false)
            }
        }

        next()
    })
}

/**
 * 检查路由是否有权限
 */
export function checkRoutePermission(route: RouteLocationNormalized): boolean {
    const licenseStore = useLicenseStore()
    const meta = route.meta as LicenseRouteMeta

    if (meta.licenseFeature && !licenseStore.hasFeature(meta.licenseFeature)) {
        return false
    }

    if (meta.licenseModule && !licenseStore.hasModule(meta.licenseModule)) {
        return false
    }

    return true
}

/**
 * 过滤有权限的路由
 */
export function filterAuthorizedRoutes(routes: any[]): any[] {
    const licenseStore = useLicenseStore()

    return routes.filter(route => {
        const meta = route.meta as LicenseRouteMeta

        if (meta?.licenseFeature && !licenseStore.hasFeature(meta.licenseFeature)) {
            return false
        }

        if (meta?.licenseModule && !licenseStore.hasModule(meta.licenseModule)) {
            return false
        }

        // 递归过滤子路由
        if (route.children) {
            route.children = filterAuthorizedRoutes(route.children)
        }

        return true
    })
}
