<template>
    <div class="waypoint-edit">
        <!-- 航点编辑操作按钮 -->
        <div class="waypoint-edit-options">
            <div v-for="option in options" :key="option.label" class="options-item" @click="addAction(option.action)">
                <img :src="option.icon" alt="" style="width: 24px; height: 24px" />
                <div class="options-item-text">{{ option.label }}</div>
            </div>
        </div>
        <!-- 航点编辑标题 -->
        <div class="waypoint-edit-header">
            <div>航点编辑</div>
            <div style="cursor: pointer">
                <el-icon size="20" @click="shipLanesStore.changeWaypointEdit(false)"><Close /></el-icon>
            </div>
        </div>
        <!-- 航点高度编辑 -->
        <div class="waypoint-edit-height">
            <div class="hieght-title">航点高度模式</div>
            <el-select v-model="placemarkData[currentPlacemark].useGlobalHeight" placeholder="Select" style="width: 100%" @change="changeHeightType">
                <el-tooltip
                    v-for="item in waypointType"
                    :key="item.value"
                    effect="dark"
                    :content="item.description"
                    placement="right"
                    class="box-item"
                >
                    <el-option :label="item.label" :value="item.value" />
                </el-tooltip>
            </el-select>
            <div class="flex items-center" v-if="placemarkData[currentPlacemark].useGlobalHeight == 0">
                <div class="bottom-content">
                    <div class="bottom-content-item" @click="globalHeight -= 100">-100</div>
                    <div class="bottom-content-item" @click="globalHeight -= 10">-10</div>
                    <div class="flex">
                        <el-input v-model="globalHeight" :min="0" class="input-item bottom-content-item" />
                        <span>m</span>
                    </div>
                    <div class="bottom-content-item" @click="globalHeight += 10">+10</div>
                    <div class="bottom-content-item" @click="globalHeight += 100">+100</div>
                </div>
            </div>
            <!-- <el-input v-model="shipLanesStore.currentWaypointParam.height" type="number" /> -->
        </div>
        <!-- 航点编辑选项卡 -->
        <div class="waypoint-edit-tabs">
            <div class="tabs-title">{{ shipLanesStore.currentPlacemark == 0 ? '起飞点' : '航点' + (shipLanesStore.currentPlacemark + 1) }}</div>
            <div class="tabs-box">
                <el-tooltip
                    v-for="(item, index) in shipLanesAction"
                    :key="index"
                    effect="dark"
                    :content="actionData[item.actionActuatorFunc].title"
                    placement="top"
                >
                    <div class="tabs-box-item" :class="{ active: currentAction === index }" @click="currentAction = index">
                        <img :src="actionData[item.actionActuatorFunc].icon" alt="" style="width: 16px; height: 16px" />
                    </div>
                </el-tooltip>
            </div>
        </div>
        <div class="waypoint-edit-content" v-if="shipLanesAction.length > 0">
            <component
                :is="actionData[shipLanesAction[currentAction].actionActuatorFunc].component"
                v-model="shipLanesAction[currentAction].actionActuatorFuncParam"
            />
        </div>
        <div class="waypoint-edit-footer" v-if="shipLanesAction.length > 0">
            <el-popconfirm title="确定要删除吗？" placement="bottom" @confirm="delAction" :hide-after="0">
                <template #reference>
                    <el-button type="danger" :icon="Delete" circle />
                </template>
            </el-popconfirm>
        </div>
    </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useShipLanes } from '/@/stores/shipLanes'
import { useMqttStore } from '/@/stores/mqtt'
import { Close, Delete } from '@element-plus/icons-vue'
import takePhoto from './shipActions/takePhoto.vue'
import startRecord from './shipActions/startRecord.vue'
import stopRecord from './shipActions/stopRecord.vue'
import hover from './shipActions/hover.vue'
import rotateYaw from './shipActions/rotateYaw.vue'
import gimbalRotate from './shipActions/gimbalRotate.vue'
import zoom from './shipActions/zoom.vue'
import { ElMessage } from 'element-plus'
const shipLanesStore = useShipLanes()
const mqttStore = useMqttStore()

// 所有动作参数
const actionActuatorFuncParams = computed(() => shipLanesStore.actionActuatorFuncParams)

// 当前选中的动作
const { currentAction, placemarkData, currentPlacemark, shipStartPoint } = storeToRefs(shipLanesStore)

// 获取当前航点动作
const shipLanesAction = computed(() => shipLanesStore.getCurrentPlacemarkAction)

// 航线信息
const form = computed(() => shipLanesStore.shipForm)

// 航线高度
const globalHeight = ref(0)

// 监听当前航点
watch(currentPlacemark, (newVal) => {
    if (newVal !== undefined) {
        globalHeight.value = placemarkData.value[newVal].executeHeight
    }
})

// 监听航点高度变化
watch(
    () => globalHeight.value,
    (newVal) => {
        // 如果新高度小于0，提示用户输入正确高度
        if (newVal < 0) {
            globalHeight.value = 0
            ElMessage.warning('航点高度不能小于0')
            return
        }
        // 如果是自定义高度，需要加上设备高度
        if (placemarkData.value[currentPlacemark.value].useGlobalHeight == 0) {
            placemarkData.value[currentPlacemark.value].executeHeight = Number(newVal)
            shipLanesStore.changeHeightModel()
        }
        console.log('当前航点信息', placemarkData.value[currentPlacemark.value])
    }
)

// 航点高度模式
const waypointType = [
    {
        label: '全局高度',
        value: '1',
        description: '所有航点高度都相同',
    },
    {
        label: '自定义高度',
        value: '0',
        description: '自定义高度',
    },
]

// 监听是否编辑航点
onMounted(() => {
    globalHeight.value = placemarkData.value[currentPlacemark.value].executeHeight
})

// 动作列表
const actionData = {
    takePhoto: {
        icon: '/img/waypoint/icon9.png',
        title: '拍照',
        component: takePhoto,
    },
    startRecord: {
        icon: '/img/waypoint/icon1.png',
        title: '开始录像',
        component: startRecord,
    },
    stopRecord: {
        icon: '/img/waypoint/icon2.png',
        title: '停止录像',
        component: stopRecord,
    },
    hover: {
        icon: '/img/waypoint/icon6.png',
        title: '悬停',
        component: hover,
    },
    rotateYaw: {
        icon: '/img/waypoint/icon7.png',
        title: '飞行器偏航角',
        component: rotateYaw,
    },
    gimbalRotate: {
        icon: '/img/waypoint/icon8.png',
        title: '云台俯仰角',
        component: gimbalRotate,
    },
    zoom: {
        icon: '/img/waypoint/icon10.png',
        title: '相机变焦',
        component: zoom,
    },
}

// 添加动作列表
const options = [
    {
        label: '拍照',
        icon: actionData.takePhoto.icon,
        action: 'takePhoto',
    },
    {
        label: '开始录像',
        icon: actionData.startRecord.icon,
        action: 'startRecord',
    },
    {
        label: '停止录像',
        icon: actionData.stopRecord.icon,
        action: 'stopRecord',
    },
    {
        label: '悬停',
        icon: actionData.hover.icon,
        action: 'hover',
    },
    {
        label: '飞行器偏航角',
        icon: actionData.rotateYaw.icon,
        action: 'rotateYaw',
    },
    {
        label: '云台俯仰角',
        icon: actionData.gimbalRotate.icon,
        action: 'gimbalRotate',
    },
    {
        label: '相机变焦',
        icon: actionData.zoom.icon,
        action: 'zoom',
    },
]

// 改变航点高度模式
const changeHeightType = (val: any) => {
    if (val == 1) {
        // 如果是全局高度
        placemarkData.value[currentPlacemark.value].executeHeight = form.value.Folder.globalHeight
    } else {
        // 如果是自定义高度
        placemarkData.value[currentPlacemark.value].executeHeight = globalHeight.value
    }
}

// 添加动作
const addAction = (action: string) => {
    shipLanesStore.addAction(action)
    currentAction.value = shipLanesAction.value.length - 1
}

// 删除动作
const delAction = () => {
    shipLanesStore.deleteAction(currentAction.value)
    if (currentAction.value > 0) {
        currentAction.value -= 1
    } else {
        currentAction.value = 0
    }
}
</script>

<style scoped lang="scss">
.waypoint-edit {
    position: absolute;
    right: 10px;
    top: 10px;
    bottom: 10px;
    width: 360px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    z-index: 99;
    background-color: #fff;
    border-radius: 12px;
    padding: 16px;

    .waypoint-edit-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 14px;
        font-weight: bold;
        border-bottom: 1px solid #e5e5e5;
        padding-bottom: 12px;
    }

    .waypoint-edit-options {
        position: absolute;
        right: 380px;
        top: 50%;
        transform: translateY(-50%);
        display: flex;
        flex-direction: column;
        gap: 10px;

        .options-item {
            display: flex;
            align-items: center;
            gap: 5px;
            cursor: pointer;
            padding: 5px 10px;
            border-radius: 5px;
            background-color: #00000080;
            transition: all 0.3s ease;

            .options-item-text {
                font-size: 14px;
                font-weight: bold;
                color: #fff;
                white-space: nowrap;
            }
        }
    }

    .waypoint-edit-height {
        display: flex;
        flex-direction: column;
        gap: 12px;

        .hieght-title {
            font-size: 14px;
            font-weight: bold;
            color: #000000;
        }

        .bottom-content {
            display: flex;
            gap: 5px;

            .bottom-content-item {
                font-size: 12px;
                background: #e4e4e4;
                border-radius: 4px;
                width: 50px;
                height: 25px;
                text-align: center;
                line-height: 25px;
                cursor: pointer;
            }

            .input-item {
                background: transparent;
            }

            :deep(.el-input__wrapper) {
                box-shadow: none;
                border: none;
                background: transparent;
            }

            :deep(.el-input__inner) {
                text-align: center;
            }
        }
    }

    .waypoint-edit-tabs {
        display: flex;
        font-size: 14px;
        font-weight: bold;
        color: #000000;
        padding-bottom: 12px;

        .tabs-title {
            height: 38px;
            line-height: 38px;
            text-align: center;
            margin-right: 10px;
        }

        .tabs-box {
            flex: 1;
            display: flex;
            flex-wrap: wrap;
            padding: 5px;
            background: #3c3c3c;
            border-radius: 5px;
            gap: 5px;

            .tabs-box-item {
                display: flex;
                align-items: center;
                justify-content: center;
                width: 28px;
                height: 28px;
                border-radius: 2px;
                cursor: pointer;

                &:hover {
                    background-color: #5cadff;
                }

                &.active {
                    background-color: #2d8cf0;
                }
            }
        }
    }

    .waypoint-edit-content {
    }

    .waypoint-edit-footer {
        display: flex;
        justify-content: flex-end;
    }

    :deep(.el-select__wrapper) {
        box-shadow: none;
        border: none;
        background: #e4e4e4;
        padding: 0 10px;
    }
}
</style>
