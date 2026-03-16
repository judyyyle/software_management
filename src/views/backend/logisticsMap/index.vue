<template>
    <div class="logistics-map-page">
        <Map :url="configUrl" :options="mapOptions" map-key="logistics-map" @onload="onMapLoaded" />

        <div class="control-panel">
            <h3>物流配送地图</h3>
            <p>固定飞行高度: {{ flightAltitude }} m</p>
            <p>当前位置: {{ currentPositionText }}</p>
            <div class="buttons">
                <el-button type="primary" @click="startFlight" :disabled="isFlying">开始飞行</el-button>
                <el-button @click="pauseFlight" :disabled="!isFlying">暂停</el-button>
                <el-button @click="resetFlight">重置</el-button>
            </div>
            <div class="legend">
                <span><i class="dot warehouse"></i>仓库</span>
                <span><i class="dot charging"></i>充电站</span>
                <span><i class="dot drone"></i>无人机</span>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import * as mars3d from 'mars3d'
import Map from '/@/views/backend/flightSpace/map/index.vue'

interface FacilityPoint {
    id: string
    name: string
    type: 'warehouse' | 'charging'
    lng: number
    lat: number
}

const configUrl = 'config/mapConfig.json'
const flightAltitude = 120
const mapOptions = {
    scene: {
        center: { lat: 30.6666, lng: 104.0722, alt: 3200, heading: 0, pitch: -52 },
    },
    globe: {
        depthTestAgainstTerrain: true,
    },
}

const facilities: FacilityPoint[] = [
    { id: 'WH-01', name: '中心仓', type: 'warehouse', lng: 104.0728, lat: 30.6658 },
    { id: 'WH-02', name: '北区仓', type: 'warehouse', lng: 104.0605, lat: 30.6791 },
    { id: 'CS-01', name: '充电站 A', type: 'charging', lng: 104.0862, lat: 30.6752 },
    { id: 'CS-02', name: '充电站 B', type: 'charging', lng: 104.0945, lat: 30.6589 },
]

const routeNodes: Array<[number, number]> = [
    [104.0728, 30.6658],
    [104.0862, 30.6752],
    [104.0945, 30.6589],
    [104.0605, 30.6791],
    [104.0728, 30.6658],
]

const mapRef = ref<mars3d.Map>()
const siteLayer = ref<mars3d.layer.GraphicLayer>()
const routeLayer = ref<mars3d.layer.GraphicLayer>()
const droneLayer = ref<mars3d.layer.GraphicLayer>()
const dronePath = ref<mars3d.graphic.PathEntity>()

const isFlying = ref(false)
const currentPosition = ref<[number, number, number]>([routeNodes[0][0], routeNodes[0][1], flightAltitude])
const currentPositionText = computed(() => {
    const [lng, lat, alt] = currentPosition.value
    return `${lng.toFixed(6)}, ${lat.toFixed(6)}, ${alt.toFixed(0)}m`
})

let flightTimer: number | null = null
let pathPointer = 0
let interpolatedPath: Array<[number, number, number]> = []

const buildInterpolatedPath = (nodes: Array<[number, number]>, altitude: number) => {
    const points: Array<[number, number, number]> = []
    const stepPerSegment = 30

    for (let i = 0; i < nodes.length - 1; i++) {
        const [startLng, startLat] = nodes[i]
        const [endLng, endLat] = nodes[i + 1]

        for (let s = 0; s < stepPerSegment; s++) {
            const t = s / stepPerSegment
            points.push([startLng + (endLng - startLng) * t, startLat + (endLat - startLat) * t, altitude])
        }
    }

    points.push([nodes[nodes.length - 1][0], nodes[nodes.length - 1][1], altitude])
    return points
}

const addFacilityMarkers = () => {
    if (!siteLayer.value) return

    facilities.forEach((item) => {
        const isWarehouse = item.type === 'warehouse'
        const color = isWarehouse ? '#2f88ff' : '#22c55e'
        const titlePrefix = isWarehouse ? '仓库' : '充电站'

        const point = new mars3d.graphic.PointEntity({
            position: [item.lng, item.lat, 0],
            style: {
                pixelSize: 14,
                color,
                outline: true,
                outlineColor: '#ffffff',
                outlineWidth: 2,
            },
            label: {
                text: `${titlePrefix} ${item.name}`,
                font_size: 16,
                color: '#ffffff',
                outline: true,
                outlineColor: '#1f2937',
                outlineWidth: 2,
                horizontalOrigin: mars3d.Cesium.HorizontalOrigin.LEFT,
                verticalOrigin: mars3d.Cesium.VerticalOrigin.BOTTOM,
                pixelOffsetX: 12,
                pixelOffsetY: -10,
            },
            attr: item,
        })

        siteLayer.value?.addGraphic(point)
    })
}

const addRouteGraphic = () => {
    if (!routeLayer.value) return

    const routePositions = routeNodes.map(([lng, lat]) => [lng, lat, flightAltitude])
    const route = new mars3d.graphic.PolylineEntity({
        positions: routePositions,
        style: {
            width: 4,
            materialType: mars3d.MaterialType.LineFlowColor,
            materialOptions: {
                color: '#00e5ff',
                speed: 10,
                percent: 0.15,
                alpha: 0.8,
            },
        },
    })

    routeLayer.value.addGraphic(route)
}

const createDroneGraphic = () => {
    if (!droneLayer.value) return

    const [lng, lat, alt] = currentPosition.value
    dronePath.value = new mars3d.graphic.PathEntity({
        position: {
            type: 'time',
            forwardExtrapolationType: mars3d.Cesium.ExtrapolationType.HOLD,
        },
        style: {
            width: 3,
            color: '#fffb00',
            opacity: 0.9,
            leadTime: 0,
        },
        label: {
            text: '物流无人机',
            font_size: 16,
            color: '#fff7b2',
            outline: true,
            outlineColor: '#111827',
            outlineWidth: 2,
            horizontalOrigin: mars3d.Cesium.HorizontalOrigin.CENTER,
            verticalOrigin: mars3d.Cesium.VerticalOrigin.BOTTOM,
            pixelOffset: new mars3d.Cesium.Cartesian2(0, -30),
        },
        model: {
            url: '/model/dajiang.gltf',
            scale: 1,
            minimumPixelSize: 28,
        },
    })

    dronePath.value.addTimePosition(mars3d.Cesium.Cartesian3.fromDegrees(lng, lat, alt), 0.1)
    droneLayer.value.addGraphic(dronePath.value)
}

const tickFlight = () => {
    if (!dronePath.value || interpolatedPath.length === 0) return

    const point = interpolatedPath[pathPointer]
    currentPosition.value = point

    const cartesian = mars3d.Cesium.Cartesian3.fromDegrees(point[0], point[1], point[2])
    dronePath.value.addTimePosition(cartesian, 0.6)

    pathPointer = (pathPointer + 1) % interpolatedPath.length
}

const startFlight = () => {
    if (isFlying.value || !dronePath.value) return

    isFlying.value = true
    if (flightTimer !== null) {
        window.clearInterval(flightTimer)
    }
    flightTimer = window.setInterval(tickFlight, 600)
}

const pauseFlight = () => {
    isFlying.value = false
    if (flightTimer !== null) {
        window.clearInterval(flightTimer)
        flightTimer = null
    }
}

const resetFlight = () => {
    pauseFlight()

    pathPointer = 0
    currentPosition.value = [routeNodes[0][0], routeNodes[0][1], flightAltitude]

    droneLayer.value?.clear()
    createDroneGraphic()
}

const onMapLoaded = (map: mars3d.Map) => {
    mapRef.value = map

    siteLayer.value = new mars3d.layer.GraphicLayer()
    routeLayer.value = new mars3d.layer.GraphicLayer()
    droneLayer.value = new mars3d.layer.GraphicLayer()

    map.addLayer(routeLayer.value)
    map.addLayer(siteLayer.value)
    map.addLayer(droneLayer.value)

    interpolatedPath = buildInterpolatedPath(routeNodes, flightAltitude)

    addFacilityMarkers()
    addRouteGraphic()
    createDroneGraphic()

    map.flyToPositions(routeNodes.map(([lng, lat]) => [lng, lat, flightAltitude]), {
        duration: 1.5,
        radius: 1800,
    })

    startFlight()
}

onUnmounted(() => {
    pauseFlight()
})
</script>

<style scoped lang="scss">
.logistics-map-page {
    position: relative;
    width: 100%;
    height: calc(100vh - 56px);
}

.control-panel {
    position: absolute;
    top: 20px;
    right: 20px;
    width: 320px;
    padding: 16px;
    border-radius: 12px;
    background: rgba(15, 23, 42, 0.8);
    color: #e5e7eb;
    backdrop-filter: blur(8px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    z-index: 10;

    h3 {
        margin: 0 0 10px;
        color: #ffffff;
        font-size: 18px;
    }

    p {
        margin: 6px 0;
        font-size: 13px;
        line-height: 1.5;
        word-break: break-all;
    }

    .buttons {
        margin-top: 12px;
        display: flex;
        gap: 8px;
    }

    .legend {
        margin-top: 14px;
        display: grid;
        grid-template-columns: 1fr;
        gap: 6px;
        font-size: 13px;

        .dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 6px;
        }

        .warehouse {
            background: #2f88ff;
        }

        .charging {
            background: #22c55e;
        }

        .drone {
            background: #fffb00;
        }
    }
}
</style>
