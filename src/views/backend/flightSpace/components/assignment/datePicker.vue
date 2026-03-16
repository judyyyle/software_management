<template>
    <div class="date-picker">
        <div v-for="day in monthDays" :key="day" class="date-picker-item" @click="selectDate(day)" :class="{ selected: modelValue.includes(day) }">
            {{ day }}
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'

// ------------------------- 变量 -------------------------
// 父组件v-model绑定的日期数组
const props = defineProps({
    modelValue: {
        type: Array,
        default: () => [],
    },
})
// 父组件v-model绑定的日期数组
const emit = defineEmits(['update:modelValue'])

// 一个月的日期数组
const monthDays = computed(() => {
    const date = new Date()
    date.setMonth(1)
    date.setDate(0)
    return Array.from({ length: date.getDate() }, (_, i) => i + 1)
})
// ------------------------- 计算属性 -------------------------

// ------------------------- 方法 -------------------------
// 选择日期
const selectDate = (day: number) => {
    // 点击当前选择的天，取消选择
    if (props.modelValue.includes(day)) {
        emit(
            'update:modelValue',
            props.modelValue.filter((d) => d !== day)
        )
    } else {
        emit('update:modelValue', [...props.modelValue, day])
    }
}
// ------------------------- 生命周期 -------------------------
</script>

<style scoped lang="scss">
.date-picker {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-start;
    align-items: center;
    width: 100%;
    background-color: #f5f5f5;
    padding: 10px;
    border-radius: 5px;
    gap: 2px;

    .date-picker-item {
        display: flex;
        justify-content: center;
        align-items: center;
        width: calc(100% / 7 - 2px);
        height: 42px;
        cursor: pointer;
        border-radius: 5px;

        &:hover {
            background-color: #e5e5e5;
        }

        &.selected {
            background-color: #007bff;
            color: #fff;
        }
    }
}
</style>
