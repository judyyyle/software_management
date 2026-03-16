import createAxios from '/@/utils/axios'

/**
 * RTMP 流媒体管理 API
 */

/**
 * 查询推流密钥
 */
export function querySecret() {
    return createAxios({
        url: '/api/rtmp/querySecret',
        method: 'post',
    })
}

/**
 * 更新推流密钥
 */
export function updateSecret(secret: string) {
    return createAxios({
        url: '/api/rtmp/updateSecret',
        method: 'post',
        data: { secret },
    })
}

/**
 * 查询流状态
 */
export function queryStreamStatus(streamKey?: string) {
    return createAxios({
        url: '/api/rtmp/streamStatus',
        method: 'get',
        params: { streamKey },
    })
}

/**
 * 踢出流（强制断开推流）
 */
export function kickStream(streamKey: string) {
    return createAxios({
        url: '/api/rtmp/kickStream',
        method: 'delete',
        data: { streamKey },
    })
}

/**
 * 获取流列表
 */
export function getStreamList() {
    return createAxios({
        url: '/api/rtmp/streamList',
        method: 'get',
    })
}
