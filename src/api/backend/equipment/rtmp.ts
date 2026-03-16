import createAxios from '/@/utils/axios'

/**
 * 获取设备RTMP直播配置
 * @param sn 设备SN（机场或飞行器）
 */
export function getRtmpConfig(sn: string) {
    return createAxios({
        url: '/admin/equipment/getRtmpConfig',
        method: 'get',
        params: { sn },
    })
}

/**
 * 更新设备RTMP配置
 */
export function updateRtmpConfig(data: {
    sn: string
    cabin_stream_key?: string
    cabin_secret?: string
    drone_stream_key?: string
    drone_secret?: string
}) {
    return createAxios({
        url: '/admin/equipment/updateRtmpConfig',
        method: 'post',
        data,
    })
}

/**
 * RTMP配置响应类型
 */
export interface RtmpConfigResponse {
    gateway_sn: string
    drone_sn: string
    cabin: {
        stream_key: string
        secret: string
        push_url: string
        play_url: {
            flv: string
            hls: string
        }
    }
    drone: {
        stream_key: string
        secret: string
        push_url: string
        play_url: {
            flv: string
            hls: string
        }
    }
    srs_config: {
        rtmp_server: string
        http_server: string
    }
}
