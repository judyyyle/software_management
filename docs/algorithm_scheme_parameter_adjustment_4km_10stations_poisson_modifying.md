# RH-ALNS + CMRAPPO 新调度算法接入实施方案（4km × 4km，10站点，泊松动态订单）

## 0. 文档定位与本次修正原则

本方案用于把 `docs/algorithm_scheme_parameter_adjustment_4km_10stations_poisson.md` 中的参数调整思路，进一步细化并落到当前项目代码实际上已经存在的配置链路上。

本次修正遵循 4 条原则：

1. 现有代码已经有真实入口的参数，必须优先使用当前项目中的真实字段名。
2. 旧文档中的抽象参数名，如果和当前代码不一致，必须先做“字段映射修正”再写入方案。
3. 当前项目还没有实现入口、但 RH-ALNS + CMRAPPO 落地又确实需要的参数，要明确标注为“新增配置”，不能误写成现有参数。
4. 4km × 4km 地图尺度在当前项目里主要由 `bbox` / `scene_id` 表达，而不是运行时显式传 `map_width_m/map_height_m`。

因此，本方案会把参数分成 4 类：

- 当前后端已真实生效
- 当前仅前端静态订单生成生效
- 当前接口已传递但新算法尚未消费
- 当前代码不存在，需要新增配置入口

---

## 1. 现有代码基线（已可复用）

### 1.0 `backend/test_data/default_scene` 已有基础数据资产

当前项目已经有一套可直接复用的默认场景资产，目录为：

- `backend/test_data/default_scene/scene_config.json`
- `backend/test_data/default_scene/entities.json`
- `backend/test_data/default_scene/orders.json`
- `backend/test_data/default_scene/osm_network.geojson`
- `backend/test_data/default_scene/osm_network.xml`
- `backend/test_data/default_scene/buildings.geojson`

这意味着当前方案不应继续停留在抽象“4km × 4km、10 站点”层面，而应直接以这套默认资产作为首轮离线训练验证和 SUMO 可视化验证的统一基线。

#### 1.0.1 当前默认场景的实际组成

根据现有数据文件，默认场景的关键事实如下：

1. 场景配置
   - `scene_id = default_test_4x4km`
   - 真实地理边界来自 `scene_config.json.bounds`

2. 实体配置
   - `1` 个 depot
   - `10` 个 station
   - `1` 辆 truck
   - `12` 架 drone
   - 其中 `9` 架 `LightDrone`，`3` 架 `HeavyDrone`

3. 订单配置
   - `10` 个静态订单 `static_orders`
   - `10` 个预定义动态订单 `dynamic_orders`
   - 动态订单 `spawn_sim_s` 分布在 `120s ~ 1740s`

4. 关键默认实体参数
   - depot：`parking_slots=4`，`swap_time=90`
   - station：统一 `parking_slots=2`，统一 `swap_time=60`
   - truck：`speed=15`，`parking_slots=3`，`swap_time=90`

5. 订单数据特征
   - 静态订单载重范围约 `1.6kg ~ 16.03kg`
   - 动态订单载重范围约 `0.9kg ~ 4.5kg`
   - 静态订单 `deadline` 范围约 `1800s ~ 5400s`
   - 动态订单 `deadline_offset_s` 范围约 `1800s ~ 3600s`

#### 1.0.2 对方案的直接影响

这组默认数据意味着当前方案需要明确区分两类输入：

1. `default_scene` 是“固定 benchmark 场景资产”
   - 它已经包含固定设施、固定订单、固定 OSM 路网
   - 适合做离线训练后的首轮回放验证、消融实验和 SUMO 可视化复核

2. “泊松动态订单”是“训练环境生成机制”
   - 不应和 `orders.json` 中的 `dynamic_orders` 混为一谈
   - `dynamic_orders` 更准确的定位是“预定义动态 benchmark 订单集”

因此，后续实施中应把两者拆开：

- `default_scene/orders.json.dynamic_orders`：用于确定性 benchmark
- `order_gen_config.arrival_rate`：用于随机泊松训练/压测

#### 1.0.3 当前默认场景是更合适的 P0 输入

P0 阶段不应再以“抽象 4km 场景”表述，而应改成：

1. 直接复用 `backend/test_data/default_scene`
2. 直接复用其 `entities.json + orders.json + osm_network.xml/geojson`
3. 先在这组固定资产上验证 RH-ALNS + CMRAPPO 的离线结果
4. 再扩展到泊松随机流训练和线上接入

### 1.1 后端调度链路

当前项目已经具备一条可复用的调度执行链：

1. `POST /api/sim/init`
   - 负责接收 `entities`、`bbox`、`order_gen_config`、`initial_orders`、`scheduled_dynamic_orders`
   - 入口文件：`backend/api/routes/simulation_bp.py`

2. `OrderManager`
   - 负责泊松流订单生成、预定义动态订单注入、订单池维护
   - 文件：`backend/environment/state/order_manager.py`

3. `DispatchDecisionEngine`
   - 负责按 `solver` 调用具体求解器，并把结果落到订单/车辆/无人机状态
   - 文件：`backend/solver/decision_engine.py`

4. `DispatchSolver` 协议
   - 约束了调度器的最小接口集
   - 文件：`backend/solver/interfaces.py`

5. `SimulationEngine`
   - 负责仿真主循环、自动增量调度触发、节流
   - 文件：`backend/environment/state/sim_engine.py`

### 1.2 前端链路

当前前端也已有可复用链路：

1. `systemStore.initSim(...)`
   - 会把 `orderStore.generatorConfig` 原样作为 `order_gen_config` 发给后端
   - 文件：`frontend/src/stores/system.ts`

2. `systemStore.dispatch(...)`
   - 会通过 `solver` 字段切换算法
   - 文件：`frontend/src/stores/system.ts`

3. `DispatchCenter`
   - 已能消费 `DispatchPlan` 并绘制卡车/无人机路径
   - 文件：`frontend/src/views/DispatchCenter/index.vue`

结论：

- 新算法不需要推翻现有前后端架构。
- 重点不是“重做链路”，而是“补齐 solver + 配置 + 训练/推理入口”。

---

## 2. 新算法接入的最小契约

### 2.1 必须实现的 solver 类

建议新增：

- `backend/solver/rh_alns_cmrappo_solver.py`
- 类名：`RhAlnsCmrappoSolver`

### 2.2 必须满足的 `DispatchSolver` 协议方法

必须实现以下方法：

- `dispatch(...)`
- `dispatch_incremental(...)`
- `should_replan_unfinished(...)`
- `dispatch_replan_current_state(...)`
- `get_active_contracts(...)`
- `fulfill_contract(...)`
- `build_incremental_route_from_stops(...)`

说明：

- 即使初版内部还没有完整用到全部能力，也要提供兼容实现。
- 否则现有 `DispatchDecisionEngine` / `SimulationEngine` 的增量链路会出现接口缺口。

### 2.3 必须输出的数据结构

`DispatchPlan` 至少需要正确填充：

- `allocations`
- `cost_total`
- `summary`
- `truck_routes`
- `drone_routes`

`AllocationResult` 至少需要正确填充：

- `order_id`
- `vehicle_id`
- `mode`
- `distance`
- `feasible`
- `reason`
- 无人机场景下的 `drone_id`
- 无人机场景下的 `recovery_station_id`

### 2.4 必须先补齐的环境语义闭环

旧文档主要提供了参数调优建议，但离线训练与离线验证真正落地前，还必须先把“问题定义 -> 环境语义 -> 动作约束 -> 奖励与验证指标”补成闭环。当前阶段至少要明确 4 个方面：

1. 模式 C 的合法回收动作语义
2. 错过卡车后的软失败 / 硬失败机制
3. 主目标与奖励函数的严格对应关系
4. FIFO、工位容量、等待、终端清场的状态变量与事件逻辑

### 2.4.1 模式 C 的正式语义修正

模式 C 不再保留“返回最近卡车”这种容易引发误解的表述，而应正式改写为：

- 无人机从仓库出发执行配送
- 配送完成后，不允许飞向道路中途的运动中卡车本体
- 无人机只能返回“卡车未来将经过的合法交接节点”

这里的合法交接节点只允许是：

- `depot`
- `station`

因此，“返回卡车”在本方案中的唯一合法解释是：

- 返回卡车未来路径中的某个仓库或充换电站节点
- 由该节点完成回收、换电或重新挂载

### 2.4.2 模式 C 回收动作的保留条件

对任一模式 C 返程候选回收点 `r_j`，动作至少需要同时满足以下条件，才能进入候选动作集并保留在 `action_mask` 中：

1. `r_j` 是卡车未来将经过的合法节点
   - `node_type(r_j) ∈ {station, depot}`
   - `ETA_truck(r_j) > t_deliver`

2. 无人机到达时序早于卡车
   - `ETA_uav(deliver -> r_j) + delta_safe <= ETA_truck(r_j)`
   - 其中 `delta_safe` 是防止“理论刚好赶上、实现里错过”的安全裕度

3. 无人机剩余电量足以支撑返程
   - `E_need(deliver -> r_j) + E_safe <= E_rem`
   - 其中 `E_safe` 为返程电量安全余量

### 2.4.3 模式 C 的回收优先级与保底规则

订单送达后，返程回收目标按以下顺序判断：

1. 首选满足时序与电量约束的“卡车未来合法交接节点”
2. 若不存在这样的节点，则尝试返回仓库
3. 若仓库也不可达，则直接飞往最近可达充换电站
4. 若连任何合法安全节点都不可达，则记为硬失败

这里需要特别保留你的业务语义：

- 如果无人机返程时，电量不足以支撑到最近汇合点与仓库，则直接飞往最近可达充换电站
- 如果最近可达充换电站也不可达，则不能再保留该动作，应直接进入硬失败

### 2.4.4 错过卡车后的状态与惩罚

“错过卡车”发生在无人机已经完成送货之后的返程阶段，因此要与“订单未完成”严格区分。

正式定义如下：

1. 订单状态
   - 订单保持 `delivered`
   - 不回滚为未完成

2. 失败语义
   - 失败的是“返程回收子任务”
   - 不是订单交付本身失败

3. 触发条件
   - 无人机已选择某个卡车未来合法交接节点作为返程目标
   - 但推进过程中出现 `ETA_uav(r_j) > ETA_truck(r_j)` 或卡车已离开该节点

4. 状态转移
   - 无人机进入 `fallback_recovery`
   - 重新选择最近可达合法安全节点作为后续目标

### 2.4.5 软失败与硬失败的正式定义

本方案将返程失败拆成两类：

#### A. 软失败：错过卡车但仍可自救

触发条件：

- 无人机已完成送货
- 错过原定回收节点上的卡车
- 仍可飞往某个后续合法安全节点

后续目标选择顺序：

1. 最近可达充换电站
2. 若仓库可达且更优，也可回仓库

软惩罚定义为：

- `soft_miss_penalty = lambda_miss * T_fallback`

其中：

- `T_fallback` 为无人机从“确认错过卡车”的时刻起，飞往下一个目标地所用时间
- 这与当前业务要求一致，即“无人机失败后飞往下一个目标地所用时间乘上惩罚系数”

#### B. 硬失败：空中能源失效 / 无合法安全节点可达

触发条件：

- 无人机当前不在 `station` / `truck` / `depot`
- 剩余电量不足以飞往任何下一个合法充换电站或仓库
- 或者已经出现半空中停电

硬失败定义为：

- `airborne_energy_failure`

硬惩罚应同时具有两层含义：

1. 状态层面
   - 该无人机进入不可继续执行状态
   - 后续不再参与当前 episode 内的可用无人机集合

2. 目标层面
   - 记为大惩罚项，且量级应显著高于普通等待或排队惩罚

### 2.4.6 主目标函数的统一定义

当前方案的主目标正式定义为：

- `总完成时间 + 小惩罚项 + 大惩罚项`

其中不再把距离、能耗、slack bonus 等代理项与主目标并列混写。距离和能耗仍可作为：

- 状态特征
- 候选动作可行性评估量
- 调试与诊断指标

但不应继续作为与主目标并列的第一层优化目标。

建议将主目标统一写成：

```text
J =
  T_complete
  + lambda_wait * T_wait
  + lambda_queue * T_queue
  + lambda_miss * T_fallback
  + Lambda_hard * N_hard_fail
```

其中：

- `T_complete`：订单总完成时间或总完成时长汇总，实施时必须在环境中选定唯一口径并全程保持一致
- `T_wait`：卡车与无人机互相等待时间总和；其中“无人机等待卡车”必须纳入小惩罚项
- `T_queue`：无人机在充换电站点排队的总时间
- `T_fallback`：错过卡车后飞往下一个站点或仓库所用总时间
- `N_hard_fail`：无人机半空停电或剩余电量不足以飞往任何合法安全节点的事件数

这里的设计要求是：

1. 小惩罚项必须是主目标的组成部分，而不是游离的代理奖励
2. 大惩罚项必须明显高一个量级，防止策略通过“赌电量”换取局部时间收益
3. 离线验证指标必须直接回到这套主目标及其分解项上，而不是只看 PPO reward

### 2.4.7 必须落到环境中的状态变量与事件逻辑

为了保证上述语义可执行，环境层必须至少显式维护以下状态：

1. 无人机状态
   - `idle`
   - `flying_to_deliver`
   - `delivered`
   - `return_to_rendezvous`
   - `return_to_depot`
   - `return_to_station`
   - `queueing_at_station`
   - `charging_or_swap`
   - `fallback_recovery`
   - `recovered`
   - `airborne_energy_failure`

2. 站点状态
   - 当前占用工位数
   - FIFO 队列
   - 每架无人机入队时刻

3. 卡车状态
   - 当前节点
   - 未来路径节点序列
   - 各未来合法站点 `ETA`

### 2.4.8 必须显式处理的关键事件

环境实现不应只停留在参数层，而应最少实现以下关键事件：

1. 订单送达
   - 订单记为完成
   - 无人机进入返程决策

2. 返程回收点选择
   - 构造未来合法回收节点集合
   - 做时序与电量可行性筛选
   - 选择 rendezvous / depot / nearest reachable station / hard failure

3. 卡车到站
   - 若对应无人机已在场等待，则完成回收
   - 若无人机尚未到达，则继续按规则推进并可能触发 miss

4. 无人机到达站点
   - 若卡车在场则回收
   - 若卡车未到且允许等待，则进入等待状态
   - 若需要换电或充电，则进入 FIFO 队列

5. 错过卡车
   - 订单不回滚
   - 无人机进入 `fallback_recovery`
   - 增加软惩罚

6. 硬失败
   - 无人机进入 `airborne_energy_failure`
   - 记录大惩罚
   - 从后续可用集合中移除

### 2.4.9 FIFO、工位容量与终端清场要求

FIFO、工位容量、站点等待和终端清场不应再只作为参数存在，而应在环境中形成正式规则：

1. `parking_slots` 决定站点可同时服务的无人机数量
2. 超过容量的无人机必须进入 FIFO 队列
3. `swap_time` / `charge_time` 结束后释放工位，并唤醒队首无人机
4. 站点等待时间必须从“入队时刻到开始服务时刻”精确累计
5. episode 结束时必须清点：
   - 已完成订单
   - 超时订单
   - 仍在返程中的无人机
   - 仍在排队中的无人机
   - 仍在等待卡车的无人机
   - 已发生硬失败的无人机

### 2.4.10 对环境推进方式的要求

上述语义对 `ETA`、赶上/错过卡车、FIFO 等都高度敏感，因此离线训练和离线验证的环境层建议采用“事件驱动或至少准事件驱动”的推进方式。

不建议把这些过程全部粗化成固定大时间步近似，否则会引入大量伪 miss / 伪 catch 事件，导致：

- `action_mask` 不稳定
- reward 噪声增大
- 训练与验证结果缺乏物理一致性

---

## 3. 参数体系重构（结合当前项目实际情况修正）

## 3.1 旧文档参数名与当前项目真实字段映射

下表是本次细化里最关键的“修正层”。

| 旧文档/抽象概念 | 当前项目实际字段或入口 | 当前状态 | 修正结论 |
| --- | --- | --- | --- |
| 泊松强度 `lambda_per_min` | `order_gen_config.arrival_rate` | 后端真实生效 | 后续统一使用 `arrival_rate`，单位“单/分钟” |
| 时间窗 `window_*` | `order_gen_config.window_min/window_max` | 后端真实生效 | 保持当前字段名 |
| 重量范围 `weight_*` | `order_gen_config.weight_min/weight_max` | 后端真实生效 | 保持当前字段名 |
| 订单总量上限 | `order_gen_config.max_orders` | 后端真实生效 | 保持当前字段名 |
| 突发泊松流 | `burst_enabled/burst_multiplier/burst_duration_s` | 字段存在；后端当前使用前两者，`burst_duration_s` 仅前端静态生成器使用 | 若新算法依赖真实 burst 窗口，需要补后端实现 |
| 地图尺度 `map_width_m/map_height_m` | 当前运行时以 `bbox` / `scene_id` 表示 | 运行时无独立字段 | 不建议在现有请求体重复新增；应由 `bbox` 推导 |
| 卡车速度 `truck_speed_mps` | 卡车实体配置里的 `speed` | 已有实体字段 | 不应新增全局 `truck_speed_mps` |
| 无人机速度 `uav_speed_mps` | `light_drone.cruise_speed` / `heavy_drone.cruise_speed` | 已有配置 | 不应再抽象成新的全局字段 |
| 评分权重/业务时长 | `solver_energy.*` | 后端真实生效 | 新文档必须使用现有字段名 |
| 订单地理分布 `geo_mode/cluster_radius_km` | 前端 `OrderGeneratorConfig` 中存在 | 仅前端静态订单生成真实生效 | 当前后端泊松流订单尚未消费 |
| 订单优先级概率 `priority_*` | 前端 `OrderGeneratorConfig` 中存在 | 仅前端静态订单生成真实生效 | 当前后端泊松流订单尚未消费 |
| 队列惩罚 `lambda_queue` | 当前项目无此配置入口 | 不存在 | 应作为新算法专属训练/推理配置新增，不能写成现有字段 |

### 3.1.1 当前代码里已经确认的真实来源

1. 泊松动态订单参数来自 `order_gen_config`
   - `frontend/src/stores/system.ts` 会把 `orderStore.generatorConfig` 发送给 `/api/sim/init`
   - `backend/environment/state/order_manager.py` 的 `configure(...)` 和 `tick(...)` 实际消费这些字段

2. 预定义动态订单走独立通道
   - 字段名是 `scheduled_dynamic_orders[*].spawn_sim_s/deadline_offset_s/deadline_sim_s/...`
   - 这与“泊松流订单”是两条不同机制，建议在方案中分开写
   - 在当前项目里，`backend/test_data/default_scene/orders.json.dynamic_orders` 就属于这条“确定性 benchmark”通道

3. 全局求解器评分和业务时长参数来自 `backend/config/drone_params.yaml` 的 `solver_energy`
   - 读取入口是 `backend/config/loader.py`
   - 但这里需要进一步拆分：能耗模型与配送/操作时长继续共享，权重与惩罚参数不再与其混用
   - RH-ALNS + CMRAPPO 的目标权重、奖励项、惩罚项应单独写入新算法配置文件，而不是继续塞进旧的共享评分参数组

4. 地图尺度当前应以 `bbox` 为单一事实来源
   - 当前 `OrderManager` 也是基于 `bbox` 采样坐标
   - 因此 4km × 4km 尺度建议作为“由 bbox 推导出的派生量”，而不是新的请求体字段

## 3.2 A类：当前项目已存在、且应直接复用的参数

### 3.2.1 后端真实生效的订单流参数

以下字段当前已经由后端泊松流订单逻辑真实消费：

| 字段名 | 当前用途 | 当前实现状态 | 4km×4km、10站点、泊松场景建议值 |
| --- | --- | --- | --- |
| `arrival_rate` | 泊松到达率，单/分钟 | 已生效 | `0.4` 作为中等动态强度起点 |
| `window_min` | 时间窗下界，分钟 | 已生效 | `20` |
| `window_max` | 时间窗上界，分钟 | 已生效 | `60` |
| `weight_min` | 订单重量下界，kg | 已生效 | `0.5` |
| `weight_max` | 订单重量上界，kg | 已生效 | `5.0` |
| `max_orders` | 总订单上限 | 已生效 | `40` 作为首轮联调上限 |
| `burst_enabled` | 是否启用突发倍率 | 已生效，但后端当前是“常量倍率提升”，不是严格窗口化 burst | 默认 `false` |
| `burst_multiplier` | 突发倍率 | 已生效 | 默认 `3`，仅压力测试时开启 |

这里需要特别修正 3 个点：

1. 当前前端默认 `arrival_rate=0`
   - 这是为了“只跑静态单 + 预定义动态单”的复现实验
   - 如果本方案目标是“泊松动态订单”，就必须在实验配置里把它改成 `>0`，推荐先用 `0.4`

2. 当前后端并没有真正按 `burst_duration_s` 建立 burst 窗口
   - `OrderManager.tick(...)` 当前只在 `burst_enabled=True` 时把 `arrival_rate` 乘以 `burst_multiplier`
   - 所以旧文档里如果把 burst 当成严格的周期窗口，和当前实现不一致，需要修正

3. `scheduled_dynamic_orders` 不应与 `arrival_rate` 混写为同一参数组
   - `scheduled_dynamic_orders` 用于可复现实验基准
   - `arrival_rate` 用于随机泊松流
   - 两者可以并存，但含义不同

4. 当前 `default_scene/orders.json` 已经是一套更适合 P0 的 benchmark 数据
   - 其中 `10` 个 `static_orders` + `10` 个 `dynamic_orders` 已足够作为第一轮固定回放集
   - 因此在训练初期，不需要一开始就强依赖随机泊松流

### 3.2.1.1 基于 `default_scene` 的参数解释修正

如果本轮直接以 `backend/test_data/default_scene` 作为 P0 输入，则应按下面方式理解当前参数：

1. `arrival_rate`
   - 在 benchmark 回放阶段可以保持 `0`
   - 因为此时主要依赖 `static_orders + dynamic_orders`

2. `scheduled_dynamic_orders`
   - 应由 `orders.json.dynamic_orders` 转换得到
   - 是 P0 阶段更重要的动态订单输入

3. `max_orders`
   - 在 benchmark 回放阶段，建议直接与 `static_orders + dynamic_orders` 总量对齐
   - 以当前默认场景而言，可先按 `20` 理解，而不是优先使用随机生成上限

4. `weight_min/weight_max`、`window_min/window_max`
   - 在 benchmark 回放阶段并不主导输入
   - 这些字段主要在开启随机泊松流训练时才真正生效

### 3.2.2 当前仅前端静态订单生成器生效的字段

以下字段当前定义在 `OrderGeneratorConfig` 中，但后端泊松流订单逻辑尚未真正消费：

| 字段名 | 当前生效位置 | 当前对后端泊松流是否生效 | 修正结论 |
| --- | --- | --- | --- |
| `geo_mode` | `frontend/src/utils/orderGen.ts` | 否 | 只能用于前端静态生成订单；不能写成“后端已支持” |
| `cluster_radius_km` | `frontend/src/utils/orderGen.ts` | 否 | 同上 |
| `priority_urgent` | `frontend/src/utils/orderGen.ts` | 否 | 同上 |
| `priority_normal` | `frontend/src/utils/orderGen.ts` | 否 | 同上 |
| `priority_low` | `frontend/src/utils/orderGen.ts` | 否 | 同上 |
| `burst_duration_s` | 前端本地定时器 | 否 | 当前后端泊松流并未按此字段实现窗口 burst |

这意味着：

- 如果 RH-ALNS + CMRAPPO 的训练环境需要“聚类分布订单”“仓库附近订单”“优先级分布”，就不能只依赖当前后端 `OrderManager._create_order(...)`。
- 需要二选一：
  - 方案A：扩展后端 `OrderManager._create_order(...)`，让其真实消费 `geo_mode/cluster_radius_km/priority_*`
  - 方案B：训练和评测阶段先主要使用 `initial_orders + scheduled_dynamic_orders`

### 3.2.3 当前已存在的共享运行时评分参数

`backend/config/drone_params.yaml` 中 `solver_energy` 当前真实可用字段如下，但在本方案中需要区分“继续保留共享”的字段和“后续应迁出共享区”的字段。

| 字段名 | 当前默认值 | 含义 |
| --- | --- | --- |
| `c_dist_et` | `1.0` | 卡车距离成本权重 |
| `c_dist_uav` | `1.0` | 无人机距离成本权重 |
| `c_energy_et` | `1.0` | 卡车能耗成本权重 |
| `c_energy_uav` | `1.0` | 无人机能耗成本权重 |
| `lambda_time` | `10.0` | 时间惩罚权重 |
| `truck_energy_kwh_per_km` | `0.75` | 卡车单位里程能耗 |
| `uav_energy_model` | `alpha` | 无人机能耗模型 |
| `uav_alpha_wh_per_kg_km` | `0.24` | 线性能耗系数 |
| `allow_moving_truck_launch` | `false` | 是否允许运动中卡车起飞 |
| `truck_service_time_order_s` | `60` | 卡车交付停留时长 |
| `drone_service_time_order_s` | `30` | 无人机交付停留时长 |
| `truck_drone_launch_time_s` | `10` | 放飞操作时长 |
| `truck_drone_recover_time_s` | `10` | 回收操作时长 |

在 RH-ALNS + CMRAPPO 接入方案中，建议把这组参数拆成两部分理解：

1. 应继续保留在共享运行时配置中的字段
   - `truck_energy_kwh_per_km`
   - `uav_energy_model`
   - `uav_alpha_wh_per_kg_km`
   - `allow_moving_truck_launch`
   - `truck_service_time_order_s`
   - `drone_service_time_order_s`
   - `truck_drone_launch_time_s`
   - `truck_drone_recover_time_s`

2. 不应继续与共享配置混用、应迁入新算法专属配置中的字段
   - `c_dist_et`
   - `c_dist_uav`
   - `c_energy_et`
   - `c_energy_uav`
   - `lambda_time`

也就是说：

- 能耗模型参数、配送时长、操作时长应保留为共享基础运行时参数
- 目标权重、评分权重、奖励项、惩罚项应进入 `backend/config/rh_alns_cmrappo.yaml`
- RH-ALNS + CMRAPPO 如果还需要额外奖励项，不要继续把 `reward.lambda_queue` 之类字段硬塞进旧的共享参数区
- 更合适的做法是新增 `rh_alns_cmrappo.yaml`，把新算法专属参数单独管理

### 3.2.4 当前已经存在但应按“实际字段名”引用的实体参数

如果需要继续沿用旧文档中的速度建议，本项目里应落在以下真实字段上：

| 旧文档写法 | 当前项目正确字段 |
| --- | --- |
| `truck_speed_mps` | 卡车实体配置 `speed` |
| `uav_speed_mps.light` | `light_drone.cruise_speed` |
| `uav_speed_mps.heavy` | `heavy_drone.cruise_speed` |

也就是说：

- 旧文档里的“卡车速度建议 8.3m/s”，在本项目里应落实到卡车配置的 `speed`
- 旧文档里的“无人机速度建议 14~18m/s”，在本项目里应落实到 `drone_params.yaml` 的 `cruise_speed`

## 3.3 B类：新算法必需，但当前项目尚无正式入口的参数

这部分参数仍然需要保留，但必须明确写成“新增配置”，而不是“当前已有字段”。

建议新增：

- `backend/config/rh_alns_cmrappo.yaml`
- `backend/config/loader.py` 中新增对应强类型 loader

建议配置结构如下：

```yaml
scene:
  derive_map_size_from_bbox: true
  default_support_radius_km: 1.2

planner:
  upper_replan_interval_sec: 360
  upper_replan_new_order_trigger: 2
  upper_horizon_sec: 3600
  incremental_dispatch_min_interval_s: 2.0
  incremental_dispatch_debounce_s: 0.0
  incremental_dispatch_max_wait_s: 5.0

candidate:
  max_candidate_orders: 36
  max_candidate_recovery_per_order: 4
  max_candidate_actions: 160
  station_wait_threshold_sec: 480

alns:
  iters: 250
  destroy_ratio: 0.12
  sa_temp0: 4.0
  sa_cooling: 0.996
  operator_update_every: 40

reward:
  completion_time_coef: 1.0
  wait_penalty_coef: 0.10
  queue_penalty_coef: 0.10
  miss_fallback_time_penalty_coef: 0.15
  hard_failure_penalty_coef: 10.0
  rendezvous_eta_safe_margin_sec: 15
  energy_safe_margin_ratio: 0.10

training:
  ppo_learning_rate: 0.0001
  rollout_steps: 4096
  batch_size: 512
  entropy_coef: 0.01
  value_loss_coef: 0.5
  use_cct: true
  cct_history_len: 6

curriculum:
  station_queue_noise: 0.0
  truck_delay_noise: 0.0
  uav_failure_prob: 0.0
  swap_time_noise: 0.0
```

这里需要强调 3 点：

1. 上述 `reward` 段已经按新的主目标重写
   - 主目标是“总完成时间 + 小惩罚项 + 大惩罚项”
   - 不再把距离权重和能耗权重作为第一层主目标参数

2. 距离、能耗相关量仍然保留工程价值
   - 它们可以继续作为状态特征、可行性评估量、日志指标与离线分析指标
   - 但不再作为当前主目标的第一层优化系数

3. `rendezvous_eta_safe_margin_sec` 与 `energy_safe_margin_ratio` 必须进入配置
   - 前者用于防止“理论刚好赶上、实现里错过”
   - 后者用于防止模式 C 返程动作在电量上过于激进

### 3.3.1 4km × 4km 场景下的关键推荐值

结合旧文档和当前项目，推荐先使用下面这一组首轮联调参数：

| 参数 | 推荐值 | 理由 |
| --- | --- | --- |
| `planner.upper_replan_interval_sec` | `360` | 对应 `arrival_rate≈0.4` 时平均每轮约 2~3 个新单 |
| `planner.upper_replan_new_order_trigger` | `2` | 泊松动态下比旧方案的 4 更敏捷 |
| `planner.upper_horizon_sec` | `3600` | 4km × 4km 小图下 1 小时滚动窗更合适 |
| `scene.default_support_radius_km` | `1.2` | 10 站点覆盖 16km² 场景时较合理 |
| `candidate.max_candidate_recovery_per_order` | `4` | 比 3 更稳，又不会把动作空间拉得过大 |
| `candidate.station_wait_threshold_sec` | `480` | 站点较密，应降低“死等单站”的容忍度 |
| `candidate.max_candidate_orders` | `36` | 比 32 略放宽，适合泊松动态池波动 |
| `candidate.max_candidate_actions` | `160` | 回收候选增加后给动作空间留余量 |
| `alns.iters` | `250` | 高频重规划下每轮求解不宜过重 |
| `alns.destroy_ratio` | `0.12` | 降低破坏强度，减少高频重规划抖动 |
| `reward.rendezvous_eta_safe_margin_sec` | `15` | 给模式 C 回收动作保留最小时间安全余度 |
| `reward.energy_safe_margin_ratio` | `0.10` | 防止返程阶段过度压榨剩余电量 |
| `reward.wait_penalty_coef` | `0.10` | 将卡车/无人机互等纳入小惩罚项 |
| `reward.queue_penalty_coef` | `0.10` | 将站点排队纳入小惩罚项 |
| `reward.miss_fallback_time_penalty_coef` | `0.15` | 用于错过卡车后的返程软惩罚 |
| `reward.hard_failure_penalty_coef` | `10.0` | 对半空停电或无安全节点可达施加大惩罚 |

### 3.3.2 关于 `map_width_m/map_height_m` 的修正结论

旧文档保留这两个参数的出发点是对的，但在当前项目里需要改写表达方式：

1. 运行时不建议在 API 请求体中重复传 `map_width_m/map_height_m`
2. solver 内部应优先从 `bbox` 推导有效地图宽高
3. 如果训练脚本为了实验记录方便需要显式保存这两个值，可以把它们写进训练元数据或 `meta.json`

也就是说：

- “语义保留”
- “运行时入口改为由 `bbox` 派生”

### 3.3.3 训练参数落盘与在线锁定机制

建议把“训练时必须与在线一致的参数”固化为一份模型元数据 JSON，并与权重一同保存：

- `backend/weights/rh_alns_cmrappo/<version>/policy.pt`
- `backend/weights/rh_alns_cmrappo/<version>/meta.json`

其中 `meta.json` 不只是记录信息，而应承担 3 个职责：

1. 记录本次训练使用的关键参数快照
2. 告诉前端哪些参数在平台中必须禁改
3. 告诉后端哪些字段必须做强校验

推荐元数据结构如下：

```json
{
  "model_version": "rh_alns_cmrappo_v1",
  "solver_name": "rh_alns_cmrappo",
  "scene_constraints": {
    "bbox_width_m": 4000,
    "bbox_height_m": 4000,
    "station_count": 10
  },
  "frontend_lock_policy": {
    "exact_locked": [],
    "range_locked": {},
    "free": []
  },
  "runtime_fixed_config": {
    "scene": {},
    "planner": {},
    "candidate": {},
    "alns": {},
    "reward": {},
    "training": {}
  }
}
```

在线接入时建议采用“双保险”：

1. 前端读 `meta.json`
   - 对 `exact_locked` 字段直接禁用控件
   - 对 `range_locked` 字段限制滑条或输入范围

2. 后端在 `/api/sim/init` 和 `/api/sim/dispatch` 再次校验
   - 若用户通过调试工具篡改请求，也不能绕过锁定策略

结论：

- “参数锁定”不能只做前端 UI 禁用
- 必须做成“训练元数据 + 前端禁改 + 后端强校验”的一体机制

### 3.3.4 在线接入时建议固定的前端可变参数

当前项目里，真正会从前端进入在线链路的可变参数主要有两包：

1. `order_gen_config`
2. `entities`

它们都会在 `systemStore.initSim(...)` 时发往后端，因此如果这些字段属于训练环境定义的一部分，就应被锁定。

#### A. 建议 `exact_locked` 的前端参数

这类参数一旦变化，就会明显改变订单分布、设施能力或车队能力，首版在线接入建议完全固定。

1. `order_gen_config` 中建议固定：
   - `arrival_rate`
   - `weight_min`
   - `weight_max`
   - `window_min`
   - `window_max`
   - `max_orders`
   - `burst_enabled`
   - `burst_multiplier`
   - `burst_duration_s`

2. `entities.depots[*]` 中建议固定：
   - `lng`
   - `lat`
   - `altitude`
   - `capacity`
   - `swap_time`
   - `parking_slots`

3. `entities.stations[*]` 中建议固定：
   - `lng`
   - `lat`
   - `altitude`
   - `swap_time`
   - `parking_slots`

4. `entities.trucks[*]` 中建议固定：
   - `speed`
   - `max_inventory`
   - `swap_time`
   - `parking_slots`
   - `home_depot_id`

5. `entities.drones[*]` 中建议固定：
   - `drone_type`
   - `home_type`
   - `home_id`

6. 场景级别建议固定：
   - `bbox`
   - `scene_id`
   - 仓库数量
   - 站点数量
   - 卡车数量
   - 无人机数量

7. 算法入口建议固定：
   - `dispatchSolver`

#### B. 建议 `conditional_locked` 的前端参数

这类字段当前前端可改，但后端泊松流暂未真实消费；是否锁定，取决于训练时是否仍依赖前端静态订单生成器。

- `geo_mode`
- `cluster_radius_km`
- `priority_urgent`
- `priority_normal`
- `priority_low`

建议规则：

1. 如果离线训练阶段使用了前端预生成订单、静态订单或其同构逻辑，这组字段也应进入锁定集
2. 如果线上完全只依赖后端泊松流，且后端仍不消费这些字段，可先不做强锁，但应写入 `meta.json` 并标记为 `frontend_only`

#### C. 建议 `free` 的前端参数

这类参数不改变调度问题本身，只影响运行体验或展示，可继续开放：

- `speedRatio`
- 名称类字段：
  - `DepotConfig.name`
  - `StationConfig.name`
  - `TruckConfig.name`

### 3.3.5 在线接入时必须固定的内部参数

除了前端字段，以下内部参数更应与模型权重一同锁定：

1. 模型结构参数
   - `training.use_cct`
   - `training.cct_history_len`
   - 以及后续新增的 `model.d_model/nhead/ff_dim/lstm_hidden/lstm_layers/hist_len/dropout`

2. 候选集与动作空间参数
   - `scene.default_support_radius_km`
   - `candidate.max_candidate_orders`
   - `candidate.max_candidate_recovery_per_order`
   - `candidate.max_candidate_actions`
   - `candidate.station_wait_threshold_sec`

3. 上层规划与 ALNS 参数
   - `planner.upper_replan_interval_sec`
   - `planner.upper_replan_new_order_trigger`
   - `planner.upper_horizon_sec`
   - `planner.incremental_dispatch_min_interval_s`
   - `planner.incremental_dispatch_debounce_s`
   - `planner.incremental_dispatch_max_wait_s`
   - `alns.iters`
   - `alns.destroy_ratio`
   - `alns.sa_temp0`
   - `alns.sa_cooling`
   - `alns.operator_update_every`

4. 奖励/惩罚参数
   - `reward.*`

5. 物理与业务时长参数
   - `light_drone.*`
   - `heavy_drone.*`
   - `solver_energy.*`

这些参数不应依赖前端传入，而应由后端在加载模型时直接从本地配置和 `meta.json` 固定读取。

## 3.4 C类：研究扩展参数（可选，但建议保留）

以下字段当前项目没有现成入口，但对课程学习与鲁棒性实验有价值：

- `curriculum.station_queue_noise`
- `curriculum.truck_delay_noise`
- `curriculum.uav_failure_prob`
- `curriculum.swap_time_noise`
- `training.use_cct`
- `training.cct_history_len`

保留原因：

- 这是 RH-ALNS + CMRAPPO 训练阶段的重要扩展位
- 不应因为当前线上推理暂未用到就从方案里删掉

---

## 4. 训练与上线双阶段落地方案

## 4.1 阶段A：离线训练

建议新增以下文件：

1. `backend/training/train_cmrappo.py`
   - 训练主入口

2. `backend/training/env_adapter.py`
   - 把当前仿真状态映射成 PPO 训练环境

3. `backend/config/rh_alns_cmrappo.yaml`
   - 承载本方案第 3.3、3.4 节的新增参数

4. `backend/weights/rh_alns_cmrappo/<version>/policy.pt`
   - 保存模型权重

5. `backend/weights/rh_alns_cmrappo/<version>/meta.json`
   - 保存训练配置摘要、地图尺度、版本信息
   - 保存前端锁定策略、在线校验规则、运行时固定参数快照

建议先形成最小闭环：

- 能读取 `bbox`
- 能读取 `order_gen_config`
- 能生成训练 episode
- 能导出首版权重
- 能导出 `meta.json` 并供前端/后端在线接入时复用
- 能按第 2.4 节的模式语义、失败机制和事件逻辑稳定推进

### 4.1.1 离线优先的 SUMO 可视化验证链路

考虑到当前前后端联调链路仍可能存在误差，建议在正式接入现有前端前，增加一条“离线训练 + SUMO 可视化复核”的中间路径。

建议流程如下：

1. 输入一张 4km × 4km 的 SUMO 路网
   - 可使用现有 `grid.net.xml`
   - 也可使用真实路网 `roads.net.xml`

2. 复用当前仓库/充电站分布算法，生成 depot / station 坐标
   - 当前已有前端版 `gridLayoutSplit(...)`
   - 建议在后端或离线脚本侧补一个 Python 同构实现，避免依赖前端

3. 将 depot / station 坐标写入 SUMO 附加文件
   - 推荐输出 `poi.add.xml` 或其他 additional file
   - 在 `sumo-gui` 中先检查设施分布是否符合 4km × 4km、10站点场景预期

4. 离线推理或离线回放调度结果
   - 先不依赖现有 Web 前端
   - 直接在 SUMO 中观察设施位置、任务分配结果、路径表现和场景合理性

5. 只有当离线训练结果和 SUMO 复核链路稳定后，再进入现有前后端接入

这样做的意义是：

- 先验证“算法 + 参数 + 场景”是否成立
- 暂时把“前后端联调误差”从核心算法验证中隔离出去
- 把问题拆成两步：先验证算法，再验证平台接入

### 4.1.2 基于 `default_scene` 的离线验证输入顺序

在当前项目条件下，离线验证建议直接按以下顺序使用现有资产：

1. 第一层：固定场景资产
   - `scene_config.json`
   - `entities.json`
   - `osm_network.xml` / `osm_network.geojson`

2. 第二层：固定 benchmark 订单
   - `orders.json.static_orders`
   - `orders.json.dynamic_orders`

3. 第三层：随机训练扩展
   - 在固定 benchmark 跑通后，再启用 `arrival_rate > 0` 的泊松流生成

这样安排的原因是：

- 当前 `default_scene` 已经足够支持首轮验证，不应放着不用
- 固定订单集更适合做算法回放、结果对比、调参回归测试
- 泊松流更适合在第二阶段作为鲁棒性训练和压力扩展

## 4.2 阶段B：在线推理接入

建议新增以下接入步骤：

1. 实现 `RhAlnsCmrappoSolver`
2. 在 `backend/solver/factory.py` 注册 `rh_alns_cmrappo`
3. 在前端 `systemStore.dispatch(...)` 扩展 solver 联合类型
4. 在 `DispatchCenter` 算法选择项中加入 `rh_alns_cmrappo`
5. 前端在进入“模型模式”时读取 `meta.json`，按锁定策略禁用对应控件
6. 后端在 `/api/sim/init` 和 `/api/sim/dispatch` 时校验锁定字段
7. 用现有 `DispatchPlan` 绘图链路直接复用可视化

---

## 5. 与当前设计的主要冲突点及修正建议

## 5.1 solver 构造参数模式

现状：

- `create_solver(name, entity_mgr)` 当前只传 `entity_mgr`

问题：

- RH-ALNS + CMRAPPO 推理通常还需要配置对象、权重目录、设备信息，有时还需要 `order_manager` 视图

建议：

1. 第一阶段尽量不改工厂签名
2. 让 `RhAlnsCmrappoSolver` 在内部自行读取 `rh_alns_cmrappo.yaml` 和权重
3. 若需要 `order_manager`，优先参考当前 `MarketBasedSolver` 的绑定方式，在创建后再绑定，而不是第一步就扩大工厂签名

## 5.2 前端 solver 枚举是固定联合类型

现状：

- `frontend/src/stores/system.ts` 的 `dispatch(...)` 只允许：
  - `greedy`
  - `greedy_mmce`
  - `greedy_mmce_bi`
  - `market`

问题：

- 新算法名无法通过类型检查，也无法在界面选择器中传递

修正建议：

- 扩展前端联合类型和调度下拉项，新增 `rh_alns_cmrappo`

## 5.3 `SimulationEngine` 的增量调度节流当前是硬编码

现状：

- 当前内部硬编码：
  - `_incremental_dispatch_min_interval_s = 2.0`
  - `_incremental_dispatch_debounce_s = 0.0`
  - `_incremental_dispatch_max_wait_s = 5.0`

问题：

- 旧文档中“根据泊松强度自适应重规划”的语义，当前代码还没有实现

修正建议：

1. 先把这 3 个值迁入 `rh_alns_cmrappo.yaml`
2. 首轮联调先用固定值
3. 第二阶段再按 `arrival_rate` 自适应计算更新

## 5.4 后端泊松流订单暂未消费 `geo_mode/priority_*`

现状：

- 当前 `OrderManager._create_order(...)` 只做 bbox 内均匀采样 + 时间窗 + 重量

问题：

- 如果训练设计依赖订单空间聚类、仓库邻近分布、优先级分布，当前后端环境并不完整

修正建议：

1. 若本轮先求最小落地，可暂不扩展后端 `OrderManager`
2. 但文档必须明确：这些字段目前只对前端静态订单生成生效
3. 如果后续训练确实依赖这些特征，再补后端生成逻辑

## 5.5 地图尺度的事实来源应统一为 `bbox`

现状：

- 初始化和调度接口都已经传 `bbox`

问题：

- 如果再单独引入运行时 `map_width_m/map_height_m`，容易出现与 `bbox` 不一致

修正建议：

- 运行时统一以 `bbox` 为准
- 训练元数据允许额外记录派生出来的宽高

## 5.6 仅做前端禁用还不够，必须有后端参数强校验

现状：

- 当前平台参数主要由前端表单控制，用户理论上可以绕过 UI 直接改请求

问题：

- 如果只在前端把输入框禁用，仍然无法保证线上推理与训练配置严格一致

修正建议：

1. 训练完成后必须导出 `meta.json`
2. 前端进入模型模式时根据 `frontend_lock_policy` 禁用或限幅参数
3. 后端读取同一份 `meta.json`，对 `order_gen_config`、`entities`、`bbox`、`solver` 做一致性校验
4. 若校验失败，接口应明确返回“模型版本与在线参数不匹配”

## 5.7 当前最大的剩余风险不是工程接入，而是环境语义未闭环

现状：

- 旧文档更偏参数调优
- 如果不先把模式 C、错过卡车、FIFO 队列和主目标写成正式规则，后续 `env_adapter`、`candidate_builder`、`validate_*` 都会各自实现一套理解

问题：

- 这会直接导致训练出来的权重与问题定义脱节
- 表面上“训练能跑通”，但离线验证和线上推理都可能出现物理语义不一致

修正建议：

1. 把第 2.4 节视为离线训练前必须冻结的环境语义契约
2. `action_mask` 必须按模式 C 合法回收条件裁剪
3. `compute_reward()` 必须与第 2.4.6 节的主目标一一对应
4. `SimulationEngine` 或离线训练环境必须显式支持 FIFO、工位容量、等待与终端清场
5. benchmark 与 stochastic 两类验证都必须单独输出：
   - miss 次数
   - fallback 次数
   - hard failure 次数
   - 站点排队时间
   - 无人机等待卡车时间

---

## 6. 实施优先级清单

## 6.1 P0（必须完成）

1. 新增 `backend/config/rh_alns_cmrappo.yaml`
2. 新增对应 loader
3. 按第 2.4 节冻结模式 C、错过卡车、主目标和 FIFO 事件逻辑
4. 新增 `train_cmrappo.py`，先形成离线训练最小闭环
5. 训练阶段导出 `backend/weights/rh_alns_cmrappo/<version>/policy.pt`
6. 训练阶段导出 `backend/weights/rh_alns_cmrappo/<version>/meta.json`
7. 直接复用 `backend/test_data/default_scene` 作为首轮固定场景输入
8. 增加“默认场景 OSM/SUMO 地图 + 当前设施分布算法”的离线场景生成链路
9. 将 depot / station 坐标写入 SUMO additional 文件，在 `sumo-gui` 中先完成离线可视化复核
10. 先基于 `orders.json.static_orders + dynamic_orders` 完成首轮离线推理/回放验证
11. benchmark 报告中必须输出 miss / fallback / hard failure / queue / wait 指标
12. 确认固定 benchmark 结果可解释、可复现后，再扩展到随机泊松训练

## 6.2 P1（强烈建议）

1. 新增 `RhAlnsCmrappoSolver`
2. 在 `backend/solver/factory.py` 注册 `rh_alns_cmrappo`
3. 让 solver 支持从权重目录加载 `policy.pt + meta.json`
4. `POST /api/sim/dispatch` 能跑通并返回标准 `DispatchPlan`
5. 把 `SimulationEngine` 节流参数配置化
6. 后端支持按 `meta.json` 强校验请求参数
7. 明确区分“训练期固定参数”和“线上可变输入参数”
8. 在固定 benchmark 稳定后，再接入 `arrival_rate > 0` 的随机泊松流训练

## 6.3 P2（增强）

1. 前端扩展 solver 枚举
2. 前端支持按 `meta.json` 锁定参数
3. 支持 `exact_locked/range_locked/free` 三档锁定策略
4. 根据 `arrival_rate` 做自适应重规划周期
5. 扩展后端 `OrderManager` 对 `geo_mode/cluster_radius_km/priority_*` 的支持
6. 接入课程学习和 CCT 扩展

---

## 7. 验收标准（Definition of Done）

## 7.1 后端验收

1. `solver=rh_alns_cmrappo` 请求返回 200
2. `DispatchPlan` 字段完整
3. 增量调度链路无接口缺失错误
4. `rh_alns_cmrappo.yaml` 配置变更能真实影响 solver 行为
5. 参数与 `meta.json` 不一致时接口会拒绝请求

## 7.2 前端验收

1. 可在 UI 中选择 `rh_alns_cmrappo`
2. 调度后能显示卡车路径和无人机路径
3. 状态统计与订单列表同步更新
4. 进入模型模式后，`exact_locked` 字段不可修改，`range_locked` 字段只能在允许范围内修改

## 7.3 训练验收

1. 训练脚本可运行并保存权重
2. 权重可被后端加载
3. 至少保存一组可复现实验元数据
4. `action_mask` 不会保留物理非法的模式 C 回收动作
5. 训练与验证报告能单独解释：
   - 错过卡车次数
   - fallback 时间
   - 站点排队时间
   - 无人机等待卡车时间
   - 硬失败次数

---

## 8. 本次细化后的结论

1. 旧文档中的参数思路可以保留，但字段名必须按当前项目修正
   - `lambda_per_min` 应统一为 `arrival_rate`
   - `truck_speed_mps` 应落实到卡车配置 `speed`
   - `uav_speed_mps` 应落实到 `cruise_speed`

2. `map_width_m/map_height_m/support_radius_km/upper_horizon_sec` 这些“算法语义参数”不应删除
   - 但其中地图宽高在当前项目里应由 `bbox` 推导，而不是作为现有请求字段宣称已经实现

3. 当前真正已被后端消费的订单流参数主要是
   - `arrival_rate`
   - `window_min/window_max`
   - `weight_min/weight_max`
   - `max_orders`
   - `burst_enabled/burst_multiplier`

4. `backend/test_data/default_scene` 当前应被视为首轮实施的主输入资产
   - 它已经具备固定场景、固定设施、固定 benchmark 订单和固定 OSM 路网
   - 因此 P0 应先围绕它建立离线验证链，而不是从抽象参数推导开始

5. 当前 `geo_mode/cluster_radius_km/priority_*` 只能算“前端静态订单生成器字段”
   - 不能在新算法实施方案里误写成“后端已支持的泊松流参数”

6. RH-ALNS + CMRAPPO 需要的新增参数，建议统一收敛到 `backend/config/rh_alns_cmrappo.yaml`
   - 这样能和现有 `backend/config/loader.py` 的风格保持一致
   - 也能避免污染全局 `solver_energy`

7. 离线训练和在线推理之间，必须增加 `meta.json` 这一层“参数契约”
   - 前端负责禁改和限幅
   - 后端负责最终强校验
   - 这样才能真正保证“训练时参数”和“上线时参数”统一

8. 当前方案中最需要补齐的不是更多调参项，而是环境语义闭环
   - 模式 C 必须限定为“返回卡车未来合法交接节点”
   - 错过卡车必须拆成软失败与硬失败
   - 主目标必须统一为“总完成时间 + 小惩罚 + 大惩罚”
   - FIFO、工位容量、等待与清场必须进入正式事件逻辑

---

## 9. 下一步建议

推荐按以下顺序推进：

1. 先冻结第 2.4 节的环境语义，并同步到 `env_adapter` / `candidate_builder` 的设计
2. 再补 `rh_alns_cmrappo.yaml + loader + 训练环境骨架`
3. 基于 `default_scene` 先跑固定 benchmark 的离线训练与离线验证
4. 用 SUMO 完成离线可视化复核
5. 固定 benchmark 稳定后，再扩展到随机泊松训练
6. 最后再做后端 solver 接入、前端枚举和参数锁定 UI

这样能先把“问题定义正确性”与“平台联调正确性”拆开，避免在环境语义尚未冻结时过早进入前后端接入阶段。
