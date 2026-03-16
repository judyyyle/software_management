/**
 * 请求追踪器
 * 追踪请求-响应对应关系
 * Requirements: 9.1, 9.2, 9.3, 9.4, 9.5
 */
import { PendingRequest, getErrorMessage } from './types'

export class RequestTracker {
    private pendingRequests: Map<string, PendingRequest> = new Map()
    private defaultTimeout: number = 30000 // 30秒

    /**
     * 生成唯一 ID
     * Requirements 9.1: 生成唯一的 tid 和 bid 用于追踪请求
     */
    generateId(): string {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = (Math.random() * 16) | 0
            const v = c === 'x' ? r : (r & 0x3) | 0x8
            return v.toString(16)
        })
    }

    /**
     * 追踪请求
     * Requirements 9.3: 请求超时未收到响应时在30秒后触发超时回调
     */
    track(tid: string, bid: string, method: string, timeout?: number): Promise<any> {
        return new Promise((resolve, reject) => {
            const timeoutMs = timeout || this.defaultTimeout

            const request: PendingRequest = {
                tid,
                bid,
                method,
                sentAt: Date.now(),
                timeout: timeoutMs,
                resolve,
                reject,
            }

            // Requirements 9.3: 设置超时
            request.timeoutId = setTimeout(() => {
                this.handleTimeout(tid)
            }, timeoutMs)

            this.pendingRequests.set(tid, request)
        })
    }

    /**
     * 处理超时
     */
    private handleTimeout(tid: string): void {
        const request = this.pendingRequests.get(tid)
        if (request) {
            this.pendingRequests.delete(tid)
            request.reject(new Error(`请求超时: ${request.method}`))
        }
    }

    /**
     * 解析响应
     * Requirements 9.2: 根据 tid 匹配对应的请求
     * Requirements 9.4: 调用请求时注册的回调函数
     * Requirements 9.5: 将错误码转换为用户可读的错误信息
     */
    resolve(tid: string, response: any): boolean {
        const request = this.pendingRequests.get(tid)
        if (!request) {
            return false
        }

        // 清除超时定时器
        if (request.timeoutId) {
            clearTimeout(request.timeoutId)
        }

        this.pendingRequests.delete(tid)

        // Requirements 9.5: 检查错误码
        const result = response?.data?.result
        if (result !== undefined && result !== 0) {
            const errorMessage = getErrorMessage(result)
            request.reject(new Error(errorMessage))
        } else {
            request.resolve(response)
        }

        return true
    }

    /**
     * 拒绝请求
     */
    reject(tid: string, error: Error): boolean {
        const request = this.pendingRequests.get(tid)
        if (!request) {
            return false
        }

        // 清除超时定时器
        if (request.timeoutId) {
            clearTimeout(request.timeoutId)
        }

        this.pendingRequests.delete(tid)
        request.reject(error)

        return true
    }

    /**
     * 获取待处理请求
     */
    getPending(): PendingRequest[] {
        return Array.from(this.pendingRequests.values())
    }

    /**
     * 获取待处理请求数量
     */
    getPendingCount(): number {
        return this.pendingRequests.size
    }

    /**
     * 检查请求是否存在
     */
    hasPending(tid: string): boolean {
        return this.pendingRequests.has(tid)
    }

    /**
     * 取消所有待处理请求
     */
    cancelAll(): void {
        for (const [tid, request] of this.pendingRequests) {
            if (request.timeoutId) {
                clearTimeout(request.timeoutId)
            }
            request.reject(new Error('请求已取消'))
        }
        this.pendingRequests.clear()
    }

    /**
     * 取消指定请求
     */
    cancel(tid: string): boolean {
        const request = this.pendingRequests.get(tid)
        if (!request) {
            return false
        }

        if (request.timeoutId) {
            clearTimeout(request.timeoutId)
        }

        this.pendingRequests.delete(tid)
        request.reject(new Error('请求已取消'))

        return true
    }

    /**
     * 设置默认超时时间
     */
    setDefaultTimeout(timeout: number): void {
        this.defaultTimeout = timeout
    }

    /**
     * 获取默认超时时间
     */
    getDefaultTimeout(): number {
        return this.defaultTimeout
    }
}
