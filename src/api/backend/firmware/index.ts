import createAxios from '/@/utils/axios'

const firmwareApi = '/admin/firmware.Version/'
const upgradeApi = '/admin/firmware.Upgrade/'

/**
 * 固件版本管理 API
 */
export function getFirmwareList(params: object = {}) {
    return createAxios({
        url: firmwareApi + 'index',
        method: 'get',
        params,
    })
}

export function addFirmware(data: object) {
    return createAxios({
        url: firmwareApi + 'add',
        method: 'post',
        data,
    })
}

export function editFirmware(data: object) {
    return createAxios({
        url: firmwareApi + 'edit',
        method: 'post',
        data,
    })
}

export function delFirmware(ids: number[]) {
    return createAxios({
        url: firmwareApi + 'del',
        method: 'delete',
        data: { ids },
    })
}

export function getAvailableFirmware(params: { device_type?: string; device_model?: string }) {
    return createAxios({
        url: firmwareApi + 'available',
        method: 'get',
        params,
    })
}

/**
 * 升级任务管理 API
 */
export function getUpgradeTaskList(params: object = {}) {
    return createAxios({
        url: upgradeApi + 'index',
        method: 'get',
        params,
    })
}

export function createUpgradeTask(data: {
    gateway_sn: string
    device_sn?: string
    upgrade_type: number
    dock_firmware_id?: number
    drone_firmware_id?: number
}) {
    return createAxios({
        url: upgradeApi + 'create',
        method: 'post',
        data,
    })
}

export function delUpgradeTask(ids: number[]) {
    return createAxios({
        url: upgradeApi + 'del',
        method: 'delete',
        data: { ids },
    })
}

export function getUpgradeTaskDetail(taskId: string) {
    return createAxios({
        url: upgradeApi + 'detail',
        method: 'get',
        params: { task_id: taskId },
    })
}

/**
 * 固件类型定义
 */
export interface Firmware {
    id: number
    device_type: 'dock' | 'drone'
    device_model: string
    version: string
    file_name: string
    file_url: string
    file_size: number
    file_size_text?: string
    md5: string
    release_note: string
    status: number
    create_time: string
    update_time: string
}

export interface UpgradeTask {
    id: number
    task_id: string
    gateway_sn: string
    device_sn: string | null
    upgrade_type: number
    upgrade_type_text?: string
    dock_firmware_id: number | null
    drone_firmware_id: number | null
    dock_version: string | null
    drone_version: string | null
    status: string
    status_text?: string
    progress: number
    current_step: string | null
    current_step_text?: string
    result_code: number | null
    error_msg: string | null
    create_time: string
    update_time: string
    finish_time: string | null
}

export const deviceTypeOptions = [
    { label: '机场', value: 'dock' },
    { label: '无人机', value: 'drone' },
]

export const deviceModelOptions = {
    dock: [
        { label: 'DJI Dock 2', value: 'Dock2' },
        { label: 'DJI Dock 3', value: 'Dock3' },
    ],
    drone: [
        { label: 'Matrice 3TD', value: 'M3TD' },
        { label: 'Matrice 3D', value: 'M3D' },
        { label: 'Matrice 30T', value: 'M30T' },
        { label: 'Matrice 30', value: 'M30' },
    ],
}

export const upgradeTypeOptions = [
    { label: '一致性升级', value: 2 },
    { label: '普通升级', value: 3 },
]

export const statusOptions = [
    { label: '已下发', value: 'sent', type: 'info' },
    { label: '执行中', value: 'in_progress', type: 'warning' },
    { label: '成功', value: 'ok', type: 'success' },
    { label: '失败', value: 'failed', type: 'danger' },
    { label: '已取消', value: 'canceled', type: 'info' },
    { label: '已拒绝', value: 'rejected', type: 'danger' },
    { label: '已暂停', value: 'paused', type: 'warning' },
    { label: '超时', value: 'timeout', type: 'danger' },
]
