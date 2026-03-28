# HiveLogix — 卡车-无人机-充换电站 多模态协同调度系统

基于离散事件仿真的城市物流"最后一公里"协同配送平台，支持卡车直递、卡-空协同、仓-空直递、动态补货、多跳中继五种履约模式，实现 100% 动态订单履约。

---

## 项目架构

> 图例：✅ 已实现 · 🔲 待实现 · 📁 目录占位

```
HiveLogix/
├── backend/                              # 后端：算法引擎与仿真系统
│   │
│   ├── api/                              # 🔲 HTTP / WebSocket 接口层
│   │   ├── routes/                       #     订单注入 / 状态查询 REST 接口
│   │   └── websockets/                   #     仿真实时推送
│   │
│   ├── core/                             # 🔲 核心领域模型
│   │   ├── entities/                     #     仓库 / 卡车 / 充换电站 / 无人机 实体类
│   │   └── models/                       #     订单模型（静态+动态，软时间窗）
│   │
│   ├── environment/
│   │   ├── simulation/                   # 🔲 离散事件仿真主循环 + 事件队列
│   │   ├── network/                      # 🔲 路网图结构 + 最短路径服务
│   │   ├── energy_model/                 # 🔲 能耗 / 充电 / 换电模型
│   │   ├── constraints/                  # 🔲 约束校验（库存/电量/时空同步）
│   │   ├── state/                        # 🔲 全局状态管理 + 时间轴控制
│   │   └── geo/                          # ✅ 地理数据服务（禁飞区识别/建筑高度）
│   │       ├── app.py                    #     Flask 入口：路由注册与请求分发
│   │       ├── data_loader.py            #     Shapefile 异步加载 + 状态管理
│   │       ├── building_service.py       #     空间裁剪 + 禁飞区阈值分类
│   │       ├── osm_service.py            #     Overpass API + OSM→GeoJSON 转换
│   │       ├── requirements.txt          #     Python 依赖（flask, geopandas 等）
│   │       ├── exporters/
│   │       │   ├── sumo_poly.py          #     SUMO .add.xml 禁飞区叠加层
│   │       │   ├── sumo_net_osm.py       #     OSM XML → SUMO net.xml
│   │       │   ├── sumo_net_grid.py      #     纯 Python 生成网格路网
│   │       │   └── geofile.py            #     GeoJSON / CSV 导出
│   │       ├── static/                   #     Flask 静态资源（旧版保留）
│   │       │   ├── css/geo_map.css
│   │       │   └── js/geo_map.js
│   │       ├── templates/
│   │       │   └── index.html            #     Flask Jinja2 模板（旧版保留）
│   │       └── shanghai_map/             #     ⚠ 数据文件（已 gitignore，需自行放置）
│   │           └── shanghai.shp / .dbf / .shx / .prj / .cpg
│   │
│   ├── solver/                           # 🔲 调度求解层
│   │   ├── fulfillment_modes/            #     五种派送模式 A~E
│   │   ├── decision_engine/              #     编排层：查货→查距/电→选模式→派单
│   │   └── algorithms/                   #     ALNS / GA / DRL
│   │
│   ├── storage/                          # 🔲 数据持久化（调度日志/仿真快照/回放）
│   ├── config/                           # 🔲 物理参数 + 算法超参 YAML
│   └── utils/                            # 🔲 通用工具（几何计算等）
│
├── frontend/                             # 前端：调度监控大屏（Vue 3 + TypeScript + Vite）
│   ├── index.html                        #   HTML 入口（CDN 载入 Bootstrap 5 / Leaflet 1.9）
│   ├── vite.config.ts                    #   开发代理：/api/* → Flask :5000
│   ├── tsconfig.json
│   ├── package.json
│   └── src/
│       ├── main.ts                       #   Vue 应用挂载
│       ├── App.vue                       #   根组件（<router-view />）
│       ├── router/
│       │   └── index.ts                  #   路由：/geo · /monitor · /analytics
│       ├── stores/                       #   Pinia 全局状态
│       │   ├── system.ts                 #     仿真运行状态（时刻/加速比/开关）
│       │   ├── entity.ts                 #     实体快照（仓库/卡车/站点/无人机）
│       │   └── order.ts                  #     订单生命周期（待派/在途/完成）
│       ├── services/
│       │   ├── http.ts                   #     统一 fetch 封装（baseURL + 错误处理）
│       │   └── websocket.ts              #     WebSocket 长连接 + 自动重连
│       ├── types/
│       │   └── index.ts                  #     全系统 TS 类型（与后端模型对齐）
│       ├── components/                   # 🔲 共享组件占位
│       │   ├── map/
│       │   ├── dashboard/
│       │   └── controls/
│       └── views/
│           ├── GeoTool/                  # ✅ UAV 禁飞区地图工具
│           │   ├── index.vue             #     主视图：状态编排 + API 调用
│           │   ├── geo_map.css           #     页面样式
│           │   └── components/
│           │       ├── LoadingMask.vue   #     数据加载进度遮罩
│           │       ├── TopBar.vue        #     顶部状态栏
│           │       ├── SideBar.vue       #     控制面板（选区/阈值/统计/导出）
│           │       └── MapView.vue       #     Leaflet 地图（建筑/道路/选区图层）
│           ├── MainMonitor/              # 🔲 实时调度大屏
│           │   └── index.vue
│           └── Analytics/               # 🔲 历史分析与回放
│               └── index.vue
│
└── docs/
    └── geo-tool.md                       # 禁飞区地图工具说明
```

---

## 快速启动

### 后端（geo 禁飞区工具）
```bash
conda activate hivelogix
cd backend/environment/geo
python app.py          # 启动于 http://localhost:5000
```

> 首次运行需将上海建筑高度 Shapefile 放入 `backend/environment/geo/shanghai_map/`
> 包含：`shanghai.shp` · `.dbf` · `.shx` · `.prj` · `.cpg`

### 前端
```bash
cd frontend
npm install            # 首次安装依赖
npm run dev            # 启动于 http://localhost:5173
```
> 前端开发服务器自动将 `/api/*` 代理到 Flask `:5000`，需后端同时运行。

## 核心实体

| 实体 | 说明 |
|---|---|
| **仓库 (Depot)** | 固定位置，库存无限，无人机起降与维护中心 |
| **卡车 (Truck)** | 路网移动，移动基站+微仓，搭载 K 架无人机，容量 $C_{truck}$ |
| **充换电站 (Station)** | 离散分布固定节点，换电模式 $\tau_{swap}$，拓展无人机作业半径 |
| **无人机 (Drone)** | 最大载重 $C_{drone}$，电池容量 $E_{max}$，能耗随距离/载重动态变化 |

## 五种履约模式

| 模式 | 名称 | 路径 |
|---|---|---|
| A | 卡车直递 | 卡车 → 客户 |
| B | 卡-空短途协同 | 卡车 → 无人机起飞 → 客户 → 无人机回收至卡车下一节点 |
| C | 仓-空直递 | 仓库 → 无人机 → 客户 → 仓库 |
| D | 空-地动态补货 | 仓库无人机 → 携货飞向卡车 → 卡车后续派送 |
| E | 多跳中继 | 卡车/仓库 → 客户 → 充换电站补能 → 追赶卡车/返回仓库 |

## 核心约束

- **库存不透支**：卡车/无人机容量实时追踪
- **时空同步**：无人机与卡车汇合必须满足位置+时间一致
- **能量守恒**：任意飞行段终点 $E_{arrive} \ge 0$，节点补能状态严格更新
- **时间惩罚**：充换电站滞留时间计入全局时间轴，影响后续汇合可行性
- **100% 履约**：所有动态订单不可拒绝，软时间窗超时产生惩罚成本
