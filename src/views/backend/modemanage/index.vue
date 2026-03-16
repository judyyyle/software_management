<template>
    <div class="modemanage">
        <!-- 页面标题 -->
        <div class="page-header">
            <h1 class="page-title">模型管理</h1>
        </div>

        <!-- 自定义按钮请使用插槽，甚至公共搜索也可以使用具名插槽渲染，参见文档 -->
        <TableSift :buttons="['daterange', 'add', 'search']" quick-search-placeholder="模型名称搜索" btn-text="添加模型" search-key="name" />

        <!-- 表格 -->
        <div class="view-table">
            <!-- 要使用 el-table 组件原有的属性，直接加在 Table 标签上即可 -->
            <Table ref="tableRef"></Table>
        </div>

        <!-- 表单 -->
        <PopupForm />
    </div>
</template>

<script setup lang="ts">
import { ref, useTemplateRef, onMounted, provide } from 'vue'
import { useI18n } from 'vue-i18n'
import TableSift from '/@/components/tableSift/index.vue'
import Table from '/@/components/table/index1.vue'
import baTableClass from '/@/utils/baTable'
import { baTableApi } from '/@/api/common'
import PopupForm from './popupForm.vue'
import { defaultOptButtons } from '/@/components/table'
// ------------------------- 生命周期 -------------------------
onMounted(() => {
    baTable.table.ref = tableRef.value
    baTable.mount()
    baTable.getData()
})
// ------------------------- 计算属性 -------------------------

// ------------------------- 变量 -------------------------
const { t } = useI18n()
const tableRef = useTemplateRef('tableRef')

let optButtons: OptButton[] = defaultOptButtons(['edit', 'delete'])

/**
 * baTable 内包含了表格的所有数据且数据具备响应性，然后通过 provide 注入给了后代组件
 */
const baTable = new baTableClass(
    new baTableApi('/admin/modemanage/'),
    {
        pk: 'id',
        column: [
            // 模型标识
            {
                label: '模型名称',
                prop: 'name',
                align: 'center',
                operator: 'LIKE',
            },
            // 所属项目
            {
                label: '所属项目',
                prop: 'project.name',
                align: 'center',
                operator: 'LIKE',
            },
            // 模型地址
            {
                label: '模型地址',
                prop: 'url',
                align: 'center',
                operator: 'LIKE',
            },
            // 模型状态
            {
                label: '模型是否显示',
                prop: 'status',
                align: 'center',
                operator: 'LIKE',
                render: 'switch',
            },
            // 创建时间
            {
                label: '创建时间',
                render: 'datetime',
                prop: 'create_time',
                operator: 'RANGE',
                timeFormat: 'yyyy-mm-dd hh:MM:ss',
            },
            // 操作
            { label: t('Operate'), align: 'center', width: 180, render: 'buttons', buttons: optButtons, fixed: 'right' },
        ],
        dblClickNotEditColumn: [undefined],
    },
    {
        defaultItems: { task_type: '0', rth_mode: '0', out_of_control_action: '0', exit_wayline_when_rc_lost: '0', wayline_precision_type: '0' },
    }
)

provide('baTable', baTable)
// ------------------------- 监听 -------------------------

// ------------------------- 方法 -------------------------
// 添加模型
const handleAdd = () => {
    // router.push('/admin/modemanage/add')
}
</script>

<style scoped lang="scss">
:deep(.el-input__wrapper) {
    box-shadow: none !important;
    background: #f7f7f7;
    border-radius: 12px;
}

.modemanage {
    background-color: #fff;
    height: 100%;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.view-table {
    flex: 1;
    overflow: auto;

    scrollbar-width: thin; /* auto | thin | none */
    scrollbar-color: #999 transparent; /* 滑块颜色 轨道颜色 */

    /* 隐藏滚动条箭头 */
    &::-webkit-scrollbar-button {
        display: none;
    }
}

/* 表格样式重置 */
:deep(.el-table) {
    border: none;
}

:deep(.el-table th) {
    background-color: #fafafa;
    color: #666;
    font-weight: 500;
    border-bottom: 1px solid #f0f0f0;
    padding: 12px 0;
}

:deep(.el-table td) {
    border-bottom: 1px solid #f0f0f0;
    padding: 12px 0;
}

:deep(.el-table tr:hover > td) {
    background-color: #fafafa;
}

:deep(.el-table::before) {
    display: none;
}

.page-header {
    padding: 15px;
}

.page-title {
    font-size: 24px;
    font-weight: bold;
    color: #333;
    margin: 0;
}
</style>
