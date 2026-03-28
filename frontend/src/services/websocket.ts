import type { WsMessage } from '@/types'

type Handler<T = unknown> = (payload: T) => void

/** WebSocket 长连接封装（仿真实时推送） */
export class WsClient {
  private ws: WebSocket | null = null
  private handlers = new Map<string, Handler[]>()
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  constructor(private url: string) {}

  connect() {
    this.ws = new WebSocket(this.url)

    this.ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as WsMessage
        const list = this.handlers.get(msg.type) ?? []
        list.forEach(h => h(msg.payload))
      } catch {
        console.warn('[WsClient] 无法解析消息:', ev.data)
      }
    }

    this.ws.onclose = () => {
      this.reconnectTimer = setTimeout(() => this.connect(), 3000)
    }
  }

  on<T>(type: string, handler: Handler<T>) {
    const list = (this.handlers.get(type) ?? []) as Handler[]
    list.push(handler as Handler)
    this.handlers.set(type, list)
  }

  send(type: string, payload: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, payload }))
    }
  }

  disconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.ws?.close()
    this.ws = null
  }
}
