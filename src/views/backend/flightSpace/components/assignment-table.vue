<template>
    <div class="assignment-table">
        <div class="tab-header">
            <div class="tab-header-left">{{ aerodromeInfo.nickname }}</div>
            <div style="cursor: pointer">
                <el-icon size="20" @click="openAssignmentPop = false"><Close /></el-icon>
            </div>
        </div>
        <div class="tab-content">
            <TableSift
                padding="15px"
                :buttons="['search', 'daterange']"
                quick-search-placeholder="搜索航线"
                btn-text="新建任务"
                search-key="airline.name"
            />
            <!-- 要使用 el-table 组件原有的属性，直接加在 Table 标签上即可 -->
            <Table ref="tableRef">
                <template #actual_time>
                    <el-table-column prop="actual_time" label="实际时间">
                        <template #default="scope">
                            <div>{{ getActualTime(scope.row) }}</div>
                        </template>
                    </el-table-column>
                </template>

                <!-- 执行状态 -->
                <template #execute_status>
                    <el-table-column prop="status" label="执行状态" width="160">
                        <template #default="scope">
                            <div class="execute-status">
                                <div
                                    class="execute-status-icon"
                                    :class="scope.row['status'] === 'ok' ? 'success' : scope.row['status'] === 'failed' ? 'error' : 'info'"
                                ></div>
                                <div>{{ execute_status[scope.row['status'] as keyof typeof execute_status] }}</div>
                                <div v-if="scope.row['total_point']">{{ `(${scope.row['now_point'] || 0}/${scope.row['total_point']})` }}</div>
                                <!-- 原因 -->
                                <el-tooltip
                                    v-if="scope.row['status'] !== 'ok' && scope.row['error_code']"
                                    effect="dark"
                                    :content="errorCode[scope.row['error_code'] as keyof typeof errorCode]"
                                    placement="top-start"
                                    style="cursor: pointer"
                                >
                                    <el-icon color="#E6A23C"><Warning /></el-icon>
                                </el-tooltip>
                            </div>
                        </template>
                    </el-table-column>
                </template>

                <!-- 媒体文件 -->
                <template #media_upload>
                    <el-table-column prop="media_upload" label="媒体文件">
                        <template #default="scope">
                            <div class="media-upload">
                                <div
                                    class="media-upload-icon"
                                    :class="
                                        scope.row['media_total'] == 0
                                            ? 'info'
                                            : scope.row['media_total'] <= scope.row['media_now']
                                              ? 'success'
                                              : 'error'
                                    "
                                ></div>
                                <!-- 状态 -->
                                <div>{{ getMediaUploadStatus(scope.row) }}</div>
                                <!-- 媒体文件数量 -->
                                <div v-if="scope.row['media_total'] > 0">
                                    {{ `(${scope.row['media_now'] || 0}/${scope.row['media_total']})` }}
                                </div>
                            </div>
                        </template>
                    </el-table-column>
                </template>
            </Table>
            <!-- 表单 -->
            <PopupForm />
        </div>
    </div>
</template>

<script setup lang="ts">
import { inject, onMounted, provide, ref, useTemplateRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Close, Warning } from '@element-plus/icons-vue'
import TableSift from '/@/components/tableSift/index.vue'
import Table from '/@/components/table/index1.vue'
import { defaultOptButtons } from '/@/components/table'
import baTableClass from '/@/utils/baTable'
import { baTableApi } from '/@/api/common'
import PopupForm from './add-assignment.vue'
import { useProjectStore } from '/@/stores/project'
import { storeToRefs } from 'pinia'
import { execute_status } from '../type/description'
import { errorCode } from '/@/config/eorr'
import { disposition } from '/@/config/disposition'
import { useMqttStore } from '/@/stores/mqtt'
import { useMapStore } from '/@/stores/map'
import { useShipLanes } from '/@/stores/shipLanes'
import { ElMessageBox, ElMessage } from 'element-plus'
import { getHxDetail } from '/@/config/flyApi'
const projectStore = useProjectStore()
const { currentProject } = storeToRefs(projectStore)

const mapStore = useMapStore()

// 航线
const shipLanesStore = useShipLanes()
const { activeShipLanes } = storeToRefs(shipLanesStore)
// 航线弹窗
const openShipLanesOptionsPop = inject<string>('openShipLanesOptionsPop')

const mqttStore = useMqttStore()
const { gateway_sn, deviceOsds, droneData } = storeToRefs(mqttStore)

watch(currentProject, () => {
    openAssignmentPop.value = false
})

defineOptions({
    name: 'flighttask',
})

onMounted(() => {
    initData()
})

const { t } = useI18n()
const tableRef = useTemplateRef('tableRef')
let optButtons: OptButton[] = defaultOptButtons([])

// 自定义一个新的按钮
let newButton: OptButton[] = [
    {
        render: 'tipButton',
        name: 'warn',
        title: '取消任务',
        type: 'danger',
        icon: 'el-icon-CircleCloseFilled',
        // 自定义点击事件
        click: (row: TableRow, field: TableColumn) => {
            ElMessageBox.confirm('确认取消任务吗？', '提示', {
                confirmButtonText: '确定',
                cancelButtonText: '取消',
                type: 'warning',
            }).then(() => {
                // 确认取消任务
                // 调用取消任务的 API 接口
                // 刷新表格数据
                // tableRef.value.refresh()
                cancelFlightTask(row.id).then((success) => {
                    if (success) {
                        // 刷新列表
                        tableRef.value.refresh()
                    }
                })
            })
        },
        display: (row: TableRow, field: TableColumn) => {
            return !row.parent_task_id
        },
    },
]

optButtons = newButton.concat(optButtons)

/**
 * baTable 内包含了表格的所有数据且数据具备响应性，然后通过 provide 注入给了后代组件
 */
const baTable = new baTableClass(
    new baTableApi('/admin/Flighttask/'),
    {
        pk: 'id',
        column: [
            // 开始时间
            {
                label: t('flighttask.execute_time'),
                render: 'datetime',
                prop: 'execute_time',
                timeFormat: 'yyyy-mm-dd hh:MM:ss',
            },
            // 结束时间
            {
                label: '执行结束时间',
                render: 'datetime',
                prop: 'end_time',
                timeFormat: 'yyyy-mm-dd hh:MM:ss',
            },
            // 实际时间
            { render: 'slot', slotName: 'actual_time' },
            // 执行状态
            { render: 'slot', slotName: 'execute_status' },
            {
                label: t('flighttask.task_type'),
                prop: 'task_type',
                align: 'center',
                render: 'tag',
                operator: 'eq',
                sortable: false,
                replaceValue: { '0': t('flighttask.task_type 0'), '1': t('flighttask.task_type 1'), '2': t('flighttask.task_type 2') },
            },
            {
                label: '计划名称',
                prop: 'name',
                align: 'center',
                operatorPlaceholder: t('Fuzzy query'),
                operator: 'LIKE',
            },
            {
                label: t('flighttask.airline__name'),
                prop: 'airline.name',
                align: 'center',
                operatorPlaceholder: t('Fuzzy query'),
                render: 'tags',
                operator: 'LIKE',
            },
            {
                label: t('flighttask.equipment__nickname'),
                prop: 'equipment.nickname',
                align: 'center',
                operatorPlaceholder: t('Fuzzy query'),
                render: 'tags',
                operator: 'LIKE',
            },
            { label: '创建人', prop: 'admin.nickname', align: 'center', operator: 'RANGE', sortable: false },
            // 媒体上传
            { render: 'slot', slotName: 'media_upload' },
            // 创建时间
            {
                label: t('flighttask.create_time'),
                render: 'datetime',
                prop: 'create_time',
                operator: 'RANGE',
                timeFormat: 'yyyy-mm-dd hh:MM:ss',
                show: false,
            },
            // 设备id
            { render: 'slot', prop: 'equipment_id', operator: 'LIKE' },
            // 操作
            { label: t('Operate'), align: 'center', width: 120, render: 'buttons', buttons: optButtons, fixed: 'right' },
        ],
        dblClickNotEditColumn: [undefined],
    },
    {
        defaultItems: { task_type: '0', rth_mode: '0', out_of_control_action: '0', exit_wayline_when_rc_lost: '0', wayline_precision_type: '0' },
    }
)

provide('baTable', baTable)
// 机场基本信息
const aerodromeInfo = inject<any>('aerodromeInfo')

watch(aerodromeInfo, () => {
    baTable.comSearch.form.equipment_id = aerodromeInfo.value.id
    baTable.table.filter!.search = [{ field: 'equipment_id', val: aerodromeInfo.value.id, operator: 'eq' }]
    baTable.getData()
})

// 打开任务弹窗
const openAssignmentPop = inject<any>('openAssignmentPop')

// 初始化数据
const initData = () => {
    baTable.table.ref = tableRef.value
    baTable.mount()
    baTable.comSearch.form.equipment_id = aerodromeInfo.value.id
    baTable.table.filter!.search = [{ field: 'equipment_id', val: aerodromeInfo.value.id, operator: 'eq' }]
    baTable.getData()?.then(() => {
        baTable.initSort()
        baTable.dragSort()
    })
}

const getMediaUploadStatus = (item: any) => {
    if (item.media_total == 0) return '无媒体文件'
    if (item.media_total <= item.media_now) return '上传成功'
    return '上传中'
}

const getActualTime = (item: any) => {
    // 开始时间
    let start_time = new Date(item.execute_time).getTime() / 1000
    // 结束时间
    let end_time = new Date(item.end_time).getTime() / 1000

    // 实际用时
    let actual_time = end_time - start_time

    if (actual_time < 0) return '0s'
    if (actual_time == 0) return '0分0秒'
    // 计算小时
    let hours = Math.floor(actual_time / 3600)
    // 计算分钟
    let minutes = Math.floor((actual_time % 3600) / 60)
    // 计算秒
    let seconds = actual_time % 60

    if (hours > 0) return `${hours}小时${minutes}分${seconds}秒`
    if (minutes > 0) return `${minutes}分${seconds}秒`
    return `${seconds}秒`
}

// 取消任务
const cancelFlightTask = async (id: string) => {
    try {
        // 发送请求
        const res = await new baTableApi('/admin/flighttask/').cancel(id + '')
        ElMessage.success(res.msg)
        baTable.getData()
    } catch (error) {
        if (error !== 'cancel') {
            ElMessage.error(error.response?.data?.msg || '取消任务失败')
        }
        return false
    }
}

// 获取航线信息
const getRouteInfo = async (id: string) => {
    try {
        // 发送请求
        const res = await getHxDetail(id)
        shipLanesStore.resetShipForm()
        // 间隔1秒
        await new Promise((resolve) => setTimeout(resolve, 1000))
        activeShipLanes.value = res.data.row.id
        const kmzJson = JSON.parse(res.data.row.kmz_json)
        shipLanesStore.showShipForm(kmzJson)
        openShipLanesOptionsPop.value = 'look'
    } catch (error) {
        console.log(error)

        // ElMessage.error(error.response?.data?.msg || '获取航线信息失败')
    }
}
</script>

<style scoped lang="scss">
.assignment-table {
    position: absolute;
    right: 10px;
    top: 10px;
    bottom: 10px;
    width: calc(100% - 310px);
    background: #fff;
    border-radius: 12px;
    z-index: 99;
    display: flex;
    flex-direction: column;

    .tab-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 14px;
        font-weight: bold;
        border-bottom: 1px solid #e5e5e5;
        padding: 12px 15px;
    }

    .tab-content {
        flex: 1;
        overflow: auto;
    }
}

.execute-status {
    display: flex;
    align-items: center;
    gap: 4px;

    .execute-status-icon {
        width: 10px;
        height: 10px;
        border-radius: 50%;

        &.success {
            background: #2ba471;
        }

        &.error {
            background: #f56c6c;
        }

        &.info {
            background: #e6a23c;
        }
    }
}
.media-upload {
    display: flex;
    align-items: center;
    gap: 4px;

    .media-upload-icon {
        width: 10px;
        height: 10px;
        border-radius: 50%;

        &.success {
            background: #2ba471;
        }

        &.error {
            background: #e6a23c;
        }

        &.info {
            background: #c5c8ce;
        }
    }
}
</style>
