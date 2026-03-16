import { defineStore } from 'pinia'
import { baTableApi } from '/@/api/common'
import { useShipLanes } from '/@/stores/shipLanes'
const shipLanesStore = useShipLanes()

export const useCockpit = defineStore('cockpit', {
    state: (): any => {
        return {
            // 弹窗
            isShow: false,
            // 当前航线信息
            currentLane: {},
            // 一键起飞任务id
            flightId: localStorage.getItem('flightId') || '',
            // 加载中
            loading: false,
            // 相机当前倍率
            cameraZoom: 1,
            // 无人机是否开机
            isDroneOn: false,
            // 是否进入指令飞行
            isCommandFlight: false,
            // 相机类型
            cameraType: 'wide',
            // 是否一键起飞
            isOneKeyTakeoff: false,
            // 是否有执行中的任务
            isExecutingTask: false,
        }
    },
    actions: {
        // 打开弹窗
        open() {
            this.isShow = true
        },
        // 关闭弹窗
        close() {
            this.isShow = false
        },
        // 一键起飞任务id
        setFlightId(id: string) {
            this.flightId = id
        },
        // 加载中
        setLoading(loading: boolean) {
            this.loading = loading
        },
        // 获取任务计划
        async getTaskPlan(equipment_id: string) {
            const res = await new baTableApi('/admin/Flighttask/').index({
                search: [
                    {
                        field: 'equipment_id',
                        val: equipment_id,
                        operator: 'eq',
                    },
                ],
            })
            // 获取正在进行的任务
            const executingTask = res.data.list.filter((item: any) => item.status === 'in_progress')
            if (executingTask.length == 0) return
            // 获取任务中的航线信息
            const airlineApi = new baTableApi('/admin/Airline/')
            const airlineRes = await airlineApi.edit({ id: executingTask[0].airline_id })
            const kmzJson = JSON.parse(airlineRes.data.row.kmz_json)
            shipLanesStore.showShipForm(kmzJson)
            this.isExecutingTask = true
            // this.currentLane = kmzJson
        },
    },
    getters: {},
})
