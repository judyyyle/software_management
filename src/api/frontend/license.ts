import createAxios from '/@/utils/axios'

// 授权服务器地址（从环境变量或配置读取）
const LICENSE_SERVER = import.meta.env.VITE_LICENSE_SERVER || ''

/**
 * 授权验证 API
 */
export const licenseApi = {
    /**
     * 激活授权
     */
    activate(licenseKey: string, fingerprint: string, hardwareInfo: object) {
        return createAxios({
            url: LICENSE_SERVER + '/api/license/activate',
            method: 'post',
            data: {
                license_key: licenseKey,
                fingerprint,
                hardware_info: hardwareInfo,
            },
        })
    },

    /**
     * 验证授权
     */
    verify(licenseKey: string, fingerprint: string, featureId?: string) {
        return createAxios({
            url: LICENSE_SERVER + '/api/license/verify',
            method: 'post',
            data: {
                license_key: licenseKey,
                fingerprint,
                feature_id: featureId,
            },
        })
    },

    /**
     * 心跳检测
     */
    heartbeat(licenseKey: string, fingerprint: string) {
        return createAxios({
            url: LICENSE_SERVER + '/api/license/heartbeat',
            method: 'post',
            data: {
                license_key: licenseKey,
                fingerprint,
            },
        })
    },

    /**
     * 获取授权信息
     */
    info(licenseKey: string, fingerprint: string) {
        return createAxios({
            url: LICENSE_SERVER + '/api/license/info',
            method: 'get',
            params: {
                license_key: licenseKey,
                fingerprint,
            },
        })
    },

    /**
     * 检查功能点授权
     */
    checkFeature(licenseKey: string, fingerprint: string, featureId: string) {
        return createAxios({
            url: LICENSE_SERVER + '/api/license/check-feature',
            method: 'post',
            data: {
                license_key: licenseKey,
                fingerprint,
                feature_id: featureId,
            },
        })
    },

    /**
     * 批量检查功能点授权
     */
    checkFeatures(licenseKey: string, fingerprint: string, featureIds: string[]) {
        return createAxios({
            url: LICENSE_SERVER + '/api/license/check-features',
            method: 'post',
            data: {
                license_key: licenseKey,
                fingerprint,
                feature_ids: featureIds,
            },
        })
    },

    /**
     * 验证 License 文件（离线模式）
     */
    verifyFile(content: string, fingerprint: string) {
        return createAxios({
            url: LICENSE_SERVER + '/api/license/verify-file',
            method: 'post',
            data: {
                content,
                fingerprint,
            },
        })
    },
}
