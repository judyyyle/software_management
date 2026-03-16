import createAxios from '/@/utils/axios'

/**
 * 版本信息接口
 */
export interface VersionInfo {
    current_version: string
    product_code: string
    php_version: string
    os_info: string
}

/**
 * 更新包接口
 */
export interface UpdatePackage {
    type: 'full' | 'incremental'
    from_version: string | null
    file_size: number
    file_size_format: string
    download_token: string
}

/**
 * 更新检查结果接口
 */
export interface UpdateCheckResult {
    has_update: boolean
    latest_version?: string
    version_code?: number
    title?: string
    update_type?: string
    is_force?: boolean
    release_notes?: string
    published_at?: string
    packages?: UpdatePackage[]
    message?: string
}

/**
 * 时间轴项接口
 */
export interface TimelineItem {
    version: string
    version_code: number
    title: string
    update_type: string
    release_notes: string
    published_at: string
    is_current: boolean
    is_latest: boolean
}


/**
 * 时间轴数据接口
 */
export interface TimelineData {
    timeline: TimelineItem[]
    current_version: string
    latest_version: string
}

/**
 * 更新历史项接口
 */
export interface HistoryItem {
    id: number
    from_version: string
    to_version: string
    status: string
    error_message: string | null
    ip_address: string
    create_time: string
}

/**
 * 更新历史数据接口
 */
export interface HistoryData {
    list: HistoryItem[]
    total: number
}

/**
 * 文件项接口
 */
export interface FileItem {
    name: string
    size: number
    crc: number
}

/**
 * 分类数据接口
 */
export interface CategoryData {
    name: string
    files: FileItem[]
    count: number
    size: number
}

/**
 * 更新包文件数据接口
 */
export interface PackageFilesData {
    package_id: number
    package_type: string
    from_version: string | null
    to_version: string
    file_count: number
    total_size: number
    files: FileItem[]
    categories: Record<string, CategoryData>
}

/**
 * 更新提醒数据接口
 */
export interface UpdateNoticeData {
    has_update: boolean
    latest_version?: string
    title?: string
    update_type?: string
    is_force?: boolean
    release_notes?: string
    published_at?: string
    packages?: UpdatePackage[]
}

const url = '/admin/system.Update/'

/**
 * 获取当前版本信息
 */
export function getVersionInfo() {
    return createAxios<VersionInfo>({
        url: url + 'info',
        method: 'get',
    })
}

/**
 * 检查更新
 */
export function checkUpdate() {
    return createAxios<UpdateCheckResult>({
        url: url + 'check',
        method: 'get',
    })
}

/**
 * 执行更新
 * @param downloadToken 下载令牌
 * @param toVersion 目标版本
 */
export function executeUpdate(downloadToken: string, toVersion: string) {
    return createAxios({
        url: url + 'execute',
        method: 'post',
        data: {
            download_token: downloadToken,
            to_version: toVersion,
        },
    })
}

/**
 * 获取版本发布时间轴
 */
export function getVersionTimeline() {
    return createAxios<TimelineData>({
        url: url + 'timeline',
        method: 'get',
    })
}

/**
 * 获取本机更新历史
 * @param page 页码
 * @param limit 每页数量
 */
export function getUpdateHistory(page: number = 1, limit: number = 20) {
    return createAxios<HistoryData>({
        url: url + 'history',
        method: 'get',
        params: { page, limit },
    })
}

/**
 * 获取更新包文件列表
 * @param downloadToken 下载令牌
 */
export function getPackageFiles(downloadToken: string) {
    return createAxios<PackageFilesData>({
        url: url + 'files',
        method: 'get',
        params: { download_token: downloadToken },
    })
}

/**
 * 获取更新提醒（仅超管）
 */
export function getUpdateNotice() {
    return createAxios<UpdateNoticeData>({
        url: url + 'notice',
        method: 'get',
    })
}

/**
 * 忽略更新提醒
 * @param version 要忽略的版本号
 */
export function dismissUpdateNotice(version: string) {
    return createAxios({
        url: url + 'dismissNotice',
        method: 'post',
        data: { version },
    })
}
