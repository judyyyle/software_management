<!-- 循环参数 -->
<template>
    <div class="cycle">
        <el-form-item label="循环类型" prop="repeat_type">
            <div class="content-item-content1">
                <div class="content-item-tab" :class="{ active: cycleForm.repeat_type === 'daily' }" @click="cycleForm.repeat_type = 'daily'">
                    每日
                </div>
                <div class="content-item-tab" :class="{ active: cycleForm.repeat_type === 'weekly' }" @click="cycleForm.repeat_type = 'weekly'">
                    每周
                </div>
                <div class="content-item-tab" :class="{ active: cycleForm.repeat_type === 'monthly' }" @click="cycleForm.repeat_type = 'monthly'">
                    每月
                </div>
            </div>
        </el-form-item>
        <!-- 选择每日执行的时间 -->
        <el-form-item label="每日执行的时间">
            <div v-for="(time, index) in repeatTimes" :key="index" class="date-box">
                <el-time-picker v-model="time.time" start="00:00" end="23:59" format="HH:mm" placeholder="请选择每日执行的时间" />
                <el-button type="danger" :icon="Delete" circle @click="removeRepeatTime(index)" />
            </div>
            <el-button type="primary" @click="addRepeatTime">添加时间</el-button>
        </el-form-item>
        <!-- 选择每周执行的天 -->
        <el-form-item label="每周执行的天" prop="repeat_config.repeat_weekdays" v-if="cycleForm.repeat_type === 'weekly'">
            <el-checkbox-group v-model="cycleForm.repeat_config.weekdays">
                <el-checkbox v-for="item in weekDays" :key="item.value" :label="item.value">{{ item.label }}</el-checkbox>
            </el-checkbox-group>
        </el-form-item>
        <!-- 选择每月执行的天 -->
        <el-form-item label="每月执行的天" v-if="cycleForm.repeat_type === 'monthly'">
            <el-popover placement="right" width="350" trigger="click">
                <template #reference>
                    <el-button type="primary" @click="showDatePicker = true">选择每月执行的天</el-button>
                </template>
                <date-picker v-model="selectedDays" />
            </el-popover>
        </el-form-item>
        <!-- 循环执行开始日期 -->
        <FormItem
            class="flex-1"
            label="循环执行开始日期"
            type="date"
            v-model="cycleForm.repeat_start_date"
            prop="repeat_start_date"
            placeholder="请选择循环执行开始日期"
            @change="handleStartDateChange"
        />
        <!-- 循环执行结束日期 -->
        <FormItem
            class="flex-1"
            label="循环执行结束日期"
            type="date"
            v-model="cycleForm.repeat_end_date"
            prop="repeat_end_date"
            placeholder="请选择循环执行结束日期"
            v-if="cycleForm.repeat_type === 'daily'"
            @change="handleEndDateChange"
        />
        <!-- 循环任务状态 -->
        <el-form-item label="循环任务状态" prop="is_repeat_enabled">
            <div class="content-item-content1">
                <div class="content-item-tab" :class="{ active: cycleForm.is_repeat_enabled === '1' }" @click="cycleForm.is_repeat_enabled = '1'">
                    启用
                </div>
                <div class="content-item-tab" :class="{ active: cycleForm.is_repeat_enabled === '0' }" @click="cycleForm.is_repeat_enabled = '0'">
                    暂停
                </div>
            </div>
        </el-form-item>
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import FormItem from '/@/components/formItem/index.vue'
import datePicker from './datePicker.vue'
import { Delete } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { timeFormat } from '/@/utils/common'

// ------------------------- 生命周期 -------------------------

// ------------------------- 计算属性 -------------------------

// ------------------------- 变量 -------------------------
// 循环参数
const cycleForm = ref({
    // 循环类型
    repeat_type: 'daily',
    // 循环配置
    repeat_config: {},
    // 循环开始时间
    repeat_start_date: '',
    // 循环结束时间
    repeat_end_date: '',
    // 是否循环执行
    is_repeat_enabled: '1',
})

// 一周x天
const weekDays = ref([
    { value: 1, label: '周一' },
    { value: 2, label: '周二' },
    { value: 3, label: '周三' },
    { value: 4, label: '周四' },
    { value: 5, label: '周五' },
    { value: 6, label: '周六' },
    { value: 7, label: '周日' },
])

// 显示日期选择器
const showDatePicker = ref(false)

// 每日执行的时间
const repeatTimes = ref<Array<{ time: string }>>([])

// 选择每月执行的天
const selectedDays = ref([])

// ------------------------- 监听 -------------------------
watch(
    () => cycleForm.value.repeat_type,
    (newVal, oldVal) => {
        if (newVal !== oldVal) {
            cycleForm.value.repeat_config = {}
        }
        // 如果不是每天就清除结束时间
        if (newVal !== 'daily') {
            cycleForm.value.repeat_end_date = ''
        }
    }
)
// ------------------------- 方法 -------------------------
// 添加每日执行的时间
const addRepeatTime = () => {
    repeatTimes.value.push({ time: '' })
}

// 删除每日执行的时间
const removeRepeatTime = (index: number) => {
    repeatTimes.value.splice(index, 1)
}

// 处理循环执行结束日期变化
const handleEndDateChange = (e) => {
    // 验证结束日期是否晚于开始日期
    if (e < cycleForm.value.repeat_start_date) {
        ElMessage.warning('循环执行结束日期不能早于开始日期')
        cycleForm.value.repeat_end_date = ''
        return
    }
    // 验证结束日期是否晚于当前日期
    if (e < new Date().toISOString().split('T')[0]) {
        ElMessage.warning('循环执行结束日期不能晚于当前日期')
        cycleForm.value.repeat_end_date = ''
        return
    }
}

// 处理循环执行开始日期变化
const handleStartDateChange = (e) => {
    // 验证开始日期是否早于结束日期
    if (e > cycleForm.value.repeat_end_date && cycleForm.value.repeat_end_date !== '') {
        ElMessage.warning('循环执行开始日期不能晚于结束日期')
        cycleForm.value.repeat_start_date = ''
        return
    }
    // 验证开始日期是否早于当前日期
    if (e < new Date().toISOString().split('T')[0]) {
        ElMessage.warning('循环执行开始日期不能早于当前日期')
        cycleForm.value.repeat_start_date = ''
        return
    }
}

// 处理每日执行时间
const handleTimeChange = () => {
    // 验证是否有时间
    if (repeatTimes.value.length === 0) {
        ElMessage.warning('请添加每日执行的时间')
        return
    }
    // 时间转换为24小时制
    cycleForm.value.repeat_config.times = []
    repeatTimes.value.forEach((item) => {
        const time = timeFormat(item.time, 'hh:MM').split(':')
        cycleForm.value.repeat_config.times.push({
            hour: time[0],
            minute: time[1],
        })
    })
}

// 处理每月执行的天
const handleMonthDaysChange = () => {
    // 验证是否有天
    if (selectedDays.value.length === 0) {
        ElMessage.warning('请选择每月执行的天')
        return
    }
    cycleForm.value.repeat_config.days = selectedDays.value
}

//抛出循环参数
const throwCycleForm = () => {
    // 处理每日执行的时间
    handleTimeChange()
    // 处理每月执行的天
    if (cycleForm.value.repeat_type === 'monthly') {
        handleMonthDaysChange()
    }
    return cycleForm.value
}

defineExpose({
    throwCycleForm,
})
</script>

<style scoped lang="scss">
.date-box {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
}

:deep(.el-input) {
    width: 100%;
}

.content-item-content1 {
    width: 100%;
    display: flex;
    padding: 2px;
    border: 1px solid #dcdcdc;
    background: #fff;
    border-radius: 6px;

    .content-item-tab {
        flex: 1;
        height: 30px;
        text-align: center;
        line-height: 30px;
        font-size: 14px;
        color: #333;
        cursor: pointer;
        border-radius: 4px;

        &.active {
            background: #00386d;
            color: #fff;
        }
    }
}
</style>
