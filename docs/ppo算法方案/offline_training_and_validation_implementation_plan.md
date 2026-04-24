# CMRAPPO（Shared PPO-Lite）离线训练与离线验证阶段实施计划

## 0. 文档目标

本文档只覆盖一个阶段：

- 离线训练
- 离线验证
- SUMO 离线可视化复核

本文档暂不覆盖：

- 现有前端接入
- 在线推理 API 接入
- 前端参数锁定 UI

但本文档要求在本阶段提前产出后续接入所需的核心文件：

- `policy.pt`
- `meta.json`
- benchmark 验证报告
- SUMO 可视化导出文件

### 0.1 本阶段核心策略：先固定 RH-ALNS，单独实现 CMRAPPO 离线训练

根据算法方案文档（`algorithm_scheme_parameter_adjustment_4km_10stations_poisson_modifying.md`）的最新修正，本阶段采用以下策略：

1. **RH-ALNS 先固定**：在本阶段，RH-ALNS 作为低频粗规划器，其输出（`CoarsePlanView`）以固定规则或简化实现提供，不在本阶段优化 ALNS 本身
2. **CMRAPPO（Shared PPO-Lite）单独实现**：本阶段的训练目标是让共享 PPO 策略在固定的 RH-ALNS 骨架约束下，学会高频局部决策
3. **可实现优先**：先跑通固定 benchmark 的完整训练-验证闭环，再扩展到随机泊松流

这样做的原因是：

- 同时优化 ALNS 和 PPO 会引入两个不稳定源，难以定位问题
- 固定 ALNS 骨架后，PPO 的输入分布更稳定，训练更容易收敛
- 先验证 PPO 的局部决策能力，再考虑 ALNS 与 PPO 的联合优化

---

## 1. 阶段目标

本阶段只回答 4 个问题：

1. 训练环境能否稳定构建并重复运行
2. 在固定 RH-ALNS 骨架约束下，CMRAPPO（Shared PPO-Lite）能否在 `backend/test_data/default_scene` 基线场景上跑出可解释结果
3. 训练好的模型能否在固定 benchmark 和随机扩展场景上离线验证
4. 能否把离线结果导出到 SUMO 做复核

---

## 2. 当前阶段唯一主输入

当前阶段统一以 `backend/test_data/default_scene` 作为主输入资产。

直接使用以下文件：

- `backend/test_data/default_scene/scene_config.json`
- `backend/test_data/default_scene/entities.json`
- `backend/test_data/default_scene/orders.json`
- `backend/test_data/default_scene/osm_network.xml`
- `backend/test_data/default_scene/osm_network.geojson`

订单使用策略如下：

1. **卡车路径规划依据（固定，不参与 PPO 训练）**
   - `orders.json.static_orders`（10 个）
   - 其中 4 个超过 HeavyDrone 上限（10kg）的订单由卡车直送（mode A），
     其余 6 个在 HeavyDrone 范围内的静态订单也可作为卡车路径节点依据
   - 第一阶段卡车路径固定，不在训练中优化

2. **PPO 训练订单源（泊松流，完全替换原始动态订单）**
   - `orders.json.dynamic_orders` 在训练阶段**不使用**，不作为背景事件注入
   - 训练时动态订单完全由泊松流生成，`arrival_rate > 0`
   - 泊松流订单重量上限设为 `heavy_drone.payload_capacity = 10kg`，
     确保所有训练订单均可由无人机处理（LightDrone ≤ 2kg，HeavyDrone ≤ 10kg）
   - 这样训练集干净，不混入固定背景订单，有利于泛化

   **泊松流订单 deadline 生成规则**（与 `backend/environment/state/order_manager.py` 保持一致）：
   - 每条泊松流订单的 deadline 由 `OrderManager._create_order` 生成：
     ```text
     deadline = spawn_time + random.uniform(window_min_s, window_max_s)
     window_min_s = window_min * 60   // 默认 window_min=20 分钟 → 1200s
     window_max_s = window_max * 60   // 默认 window_max=60 分钟 → 3600s
     ```
   - 预定义动态订单（`set_scheduled_dynamic_orders`）优先使用 `deadline_sim_s`，
     否则使用 `spawn_sim_s + deadline_offset_s`（默认 `deadline_offset_s=900s`）
   - 训练阶段统一使用 `window_min=20, window_max=60`（分钟），与现有在线参数一致；
     可通过 `rh_alns_cmrappo.yaml` 的 `scene.order_window_min_min` / `order_window_max_min` 覆盖

实施原则：

- 第一阶段：卡车路径固定 + 泊松流训练，验证 PPO 局部决策能力
- 第二阶段（离线验证）：用 `orders.json.dynamic_orders` 作为固定 benchmark 验证集，
  评估模型在真实动态订单分布上的表现

## 2.1 当前阶段必须先冻结的环境语义契约

离线训练与离线验证不能只依赖参数文件，还必须先冻结环境语义。当前阶段至少要先确定以下 4 件事：

1. 模式 C 的合法回收动作定义
2. 错过卡车后的软失败 / 硬失败机制
3. 主目标函数与奖励项的对应关系
4. FIFO、工位容量、等待、终端清场的事件逻辑

### 2.1.1 模式 C 的合法回收动作

模式 C 在离线环境中的正式语义应为：

- 无人机送达后，不允许飞向道路中途的运动中卡车
- “返回卡车”只能解释成返回卡车未来将经过的合法交接节点
- 合法交接节点仅限：
  - `station`
  - `depot`

因此，候选回收动作仅在同时满足以下条件时才能保留在 `action_mask` 中：

1. 该节点在卡车未来路径上，且仍未经过
2. 无人机预计到达时刻早于卡车到达该节点
3. 无人机剩余电量足以飞到该节点并保留安全余量

### 2.1.2 模式 C 返程的保底规则

订单送达后，返程目标按以下顺序选择：

1. 卡车未来合法交接节点
2. 仓库
3. 最近可达充换电站
4. 若上述均不可达，则进入硬失败

这里必须保留的业务语义是：

- 如果无人机返程时，电量不足以支撑到最近汇合点与仓库，则直接飞往最近可达充换电站
- 若连最近可达充换电站也不可达，则不能继续保留该动作

### 2.1.3 错过卡车后的软失败与硬失败

“错过卡车”发生在无人机送达订单之后的返程阶段，因此：

- 订单应保持为已完成
- 失败的是返程回收子任务，不是订单交付失败

软失败定义：

- 错过原定回收节点上的卡车
- 但仍能飞往某个合法安全节点

软惩罚定义：

- `soft_miss_penalty = lambda_miss * T_fallback`

其中 `T_fallback` 为无人机从确认失败时刻飞往下一个目标地所用时间。

硬失败定义：

- 无人机当前不在 `station` / `truck` / `depot`
- 且剩余电量不足以飞往任何下一个合法充换电站或仓库
- 或已经出现半空中停电

### 2.1.4 当前阶段统一采用的主目标

> **注意**：本节为早期版本，完整权威定义见 Section 4.1.4 和 4.1.4.1，以 4.1.4 为准。

当前阶段统一采用以下主目标：

```text
J =
  T_complete
  + lambda_wait * T_wait
  + wait_idle_penalty_coef * T_idle
  + lambda_queue * T_queue
  + lambda_miss * T_fallback
  + lambda_res_timeout * T_res_timeout
  + lambda_overdue * T_overdue
  + Lambda_hard * N_hard_fail
  + Lambda_hard_overdue * N_hard_overdue
```

含义如下：

- `T_complete`：所有订单从进入可调度池到完成配送的时长总和，定义为
   `T_complete = sum_i (t_delivered_i - t_spawn_i)`，**仅对无人机可配送订单统计**（`weight <= 10kg`）
   其中 `t_spawn_i` 是订单进入可调度池的时刻（对应 `spawn_sim_s`，静态订单取 `0`），
   `t_delivered_i` 是无人机完成配送的时刻（不含 `drone_service_time_order_s`，
   若有服务时长则取服务完成时刻，需与环境实现保持一致）。
   示例：3 个订单分别在进入池后耗时 10 分钟、20 分钟、40 分钟完成配送，则
   `T_complete = 70 分钟`。
   该目标只计入策略可影响的无人机订单时间段，避免将 `mode A` 卡车背景订单混入 PPO 主评估口径
- `T_wait`：物理等待时间总和，仅指 UAV 在节点处被动等待卡车到来的时间，
   不包含显式 WAIT 动作产生的 idle 时间（两者必须分开累计，不允许重复计入）
- `T_idle`：UAV 显式选择 WAIT 动作后产生的 idle 时间总和，定义为
   `T_idle = sum_k delta_t_wait_k`
   其中 `delta_t_wait_k` 是第 k 次 WAIT 动作对应的仿真时间推进量。
   即时惩罚为 `r_wait_step = -wait_idle_penalty_coef * delta_t_wait_k`，
   与 `lambda_wait` 保持同一口径（首轮建议 `wait_idle_penalty_coef = 0.10`，
   与 `lambda_wait` 相同，后续可通过 curriculum 衰减）。
   注意：排队（`T_queue`）、站点容量导致的被动等待不计入 `T_idle`，
   避免与 `T_wait` / `T_queue` 重复扣罚
- `T_queue`：无人机在充换电站排队总时间
- `T_fallback`：错过卡车后飞往下一个站点或仓库的总时间
- `T_res_timeout`：reservation timeout 造成的总局部损失
- `T_overdue`：所有未送达且已超时订单的累计超时时长，定义为
   `T_overdue = sum_i max(0, t_now - deadline_i)`（对所有未送达订单，每步累加）。
   **必须在每个仿真步实时累加，不允许等到送达时才结算**，原因是：若仅在送达时结算，
   模型会学到"不送超时订单"可规避惩罚，导致超时订单被系统性忽略。
   实时累加后，送达超时订单会停止该订单的累加，模型仍有动力去送。
   送达时不再叠加额外惩罚，避免双重计入。
   首轮建议 `lambda_overdue = 0.20`（高于 `lambda_wait`，体现超时的更高优先级）
- `N_hard_overdue`：超时时长超过 `max_overdue_sec` 仍未送达的订单数。
   超过该阈值后订单从可调度池中强制移除，记一次固定惩罚 `Lambda_hard_overdue`，
   避免模型长期持有无法挽回的"死单"。
   首轮建议 `max_overdue_sec = 600`，`Lambda_hard_overdue = 600`
- `N_hard_fail`：半空停电或无合法安全节点可达的事件数

要求：

- 训练 reward 必须来自这套主目标及其分解项
- benchmark 与 stochastic 验证也必须回到这套指标，不允许只看 PPO reward
- `mode A` 背景订单不进入这套 PPO 主指标；其执行结果只进入单独的系统上下文统计区
- `T_overdue` 的即时惩罚必须在 `env_adapter` 的 `compute_reward()` 里逐步结算，
   不能只在 episode 末端累计，否则梯度信号仍然稀释
- `T_idle` 的即时惩罚同样必须逐步结算（同上）

### 2.1.5 当前阶段建议采用事件驱动环境

由于模式 C 的赶上 / 错过、FIFO 队列与站点容量都对时间精度敏感，本阶段的离线环境建议采用事件驱动或至少准事件驱动推进，而不是大时间步粗近似。

否则会直接导致：

- 模式 C 候选动作判定失真
- `action_mask` 不稳定
- miss / catch 统计不可靠
- 奖励噪声异常增大

---

## 2.2 系统运行基础约束（必须先冻结）

以下约束是系统业务语义的基础事实，必须在实现任何环境逻辑之前明确冻结，不允许在实现过程中各自理解。

1. **无人机单次只携带一个订单**
   - 无人机每次出发只能携带并配送一个订单的货物
   - 飞行途中不能接新单，必须完成当前订单的配送与返程后才能进入下一轮决策

2. **卡车与仓库货物均充足，无人机可在卡车上或仓库直接取货**
   - 货物为同质化货物，仅有重量差别
   - 卡车货物充足，无人机在卡车上时可直接取货，无需返回仓库
   - 仓库货物同样充足，从仓库出发的无人机直接取货出发

3. **卡车上配备充换电设施，无人机在卡车上可完成充换电**
   - 无人机被卡车回收后（mode C 成功），可在卡车上充换电
   - 卡车上充换电不占用站点 FIFO 队列，由卡车服务时长（`truck_drone_recover_time_s`）决定
   - 充换电完成后，无人机进入 `riding_with_truck` 状态，等待下一个决策点

4. **"哪些无人机搭车出发"在系统启动时由外部预设决定，不是 PPO 或 ALNS 的决策范围**
   - 由于预订单的存在，卡车在系统启动时即出发
   - 哪些无人机随卡车出发、哪些从仓库直飞，是系统初始化时的外部配置，不进入训练决策空间

5. **无人机在卡车上时，取货点由当前位置自然决定**
   - 处于 `riding_with_truck` 状态的无人机，起飞时直接从卡车取货
   - 处于 `idle_at_depot` 状态的无人机，出发时从仓库取货
   - 不存在"在卡车上但要飞回仓库取货"的场景

---

## 3. 总实施顺序

建议严格按以下顺序推进：

1. 固化数据与参数契约
2. 冻结环境语义契约
3. 实现离线场景装载器
4. 实现训练环境适配器
5. 实现候选动作与上层规划骨架
6. 实现 PPO 训练主循环
7. 实现固定 benchmark 离线验证
8. 实现随机扩展离线验证
9. 实现 SUMO 可视化导出
10. 固化模型产物与阶段验收

不建议跳步，尤其不要在训练环境未稳定前提前做前端接入。

---

## 4. 分阶段实施内容

## 4.1 Phase 1：固化离线阶段的数据、参数契约与架构职责边界

目标：

- 先定义训练到底吃什么、输出什么
- 明确 RH-ALNS 与 CMRAPPO（Shared PPO-Lite）在本阶段的职责边界
- 冻结所有后续实现依赖的环境语义契约

### 4.1.1 本阶段架构职责边界（必须先冻结）

本阶段正式采用以下分工，不允许在实现过程中混淆：

**RH-ALNS（低频粗规划器，本阶段固定）**

在本阶段，RH-ALNS 以简化固定实现提供，其职责仅限于：

1. 生成 truck 的未来骨架路线（`truck_backbone_route`）
2. 给订单池做粗粒度优先级排序与窗口分桶（`order_priority_band`）
3. 生成 station/depot 支持半径与可行 recovery 池（`recovery_pool`）
4. 给未来一段时间内的固定充换电节点分配粗粒度服务软预算（`node_charge_load_budget`）
5. 提供 route drift 参考基线（`route_drift_ref`）

本阶段 RH-ALNS 的触发规则固定为：

```text
Trigger_ALNS =
    periodic(interval = coarse_replan_interval_sec)   // 默认 420s
 OR backlog_new_orders >= coarse_new_order_trigger    // 默认 3 单
 OR route_drift_ratio >= route_drift_trigger_ratio    // 默认 0.18
 OR fallback_count(window) >= fallback_burst_trigger_count  // 默认 2 次/300s
 OR hard_failure_count(window) >= hard_failure_trigger_count // 默认 1 次
```

本阶段不优化 ALNS 本身的求解质量，只要求其输出满足 `CoarsePlanView` 接口契约。

**CMRAPPO（Shared PPO-Lite，本阶段训练目标）**

CMRAPPO 是本阶段唯一需要训练的组件，其职责是：

1. 在每次局部事件触发时，基于共享参数策略在 `action_mask` 过滤后的候选集中选择动作
2. 动作空间采用 `factorized action space`，顺序固定为：
   - 先选 `order_i`（候选订单）
   - 再选 `mode_i`（`WAIT` / `B` / `C`）
   - 再选 `recover_node_j`（回收节点）
3. `mode A` 不进入 PPO actor head，由 RH-ALNS / truck-side coarse planner 管理
4. 模式约束采用双层语义：
   - `planner_mode_cap={A,B,C}`：上层规划器语义边界
   - `policy_mode_mask={WAIT,B,C}`：真正给 PPO actor 的可选模式掩码
5. 训练阶段固定使用 `centralized_train_only critic`
6. 推理阶段默认使用 `greedy` 动作选择

**PPO 决策触发时机（两类，结构完全统一）**

PPO 在以下两类时机触发，动作空间结构相同：

- **`idle` 触发**：无人机在 depot 或 station 完成充换电后进入 idle，触发 PPO 决策
- **`riding_with_truck` 触发**：卡车到达某个站点，且该站点在 `CoarsePlanView.launch_candidate_stations` 中，触发该卡车上所有 `riding_with_truck` 无人机的 PPO 决策

两类触发的动作空间完全一致：`(order_i, mode, recover_node_j)`。

`WAIT` 动作在两类触发中的语义：
- `idle` 状态下：保持当前位置不动，等待下一个环境事件
- `riding_with_truck` 状态下：继续搭车到下一站，不起飞（语义等价于 STAY_ON_TRUCK）

**`riding_with_truck` 状态下的候选集构建（实时计算）**

与 `idle` 状态不同，`riding_with_truck` 触发时的回收候选点不由 ALNS 预算，而是在 PPO 决策时根据当前时刻实时计算：

```text
当卡车到达站点 S_k（S_k ∈ launch_candidate_stations）时：
  对每个候选订单 order_i：
    t_deliver = t_now + fly_time(S_k → order_i.location)
    recovery_pool(order_i) = {
      r_j | node_type(r_j) ∈ {station, depot}
          AND ETA_truck(r_j) > t_deliver
          AND ETA_uav(order_i.location → r_j) + delta_safe <= ETA_truck(r_j)
          AND E_need(order_i.location → r_j) + E_safe <= E_rem
    }
```

这样设计的原因是：起飞时刻（`t_now`）决定了 `t_deliver`，进而决定了哪些回收节点在时序上合法。ALNS 无法在粗规划时预知精确的起飞时刻，因此回收候选点必须实时计算。

这里需要特别固定一个业务口径：

- **mode C 的等待回收合法性**只由时序、安全可达性和能量可行性决定
- **充换电容量/排队**只在 UAV 被成功回收后的服务阶段生效，不构成“等待被回收”动作本身的非法性条件
- 因此，`predicted_queue_time(node)` 可以作为候选排序、软截断或 reward 估计的输入，但不应作为 mode C 等待回收动作的硬性合法性判据

**ALNS ↔ PPO 正式接口契约（`CoarsePlanView`）**

ALNS 向 PPO 输出的只读边界对象定义如下：

```text
CoarsePlanView {
  plan_version: int
  issued_at: float
  valid_until: float
  truck_backbone_route: [node_id]
  truck_eta_map: {node_id -> eta_sec}
  authorized_orders: [order_id]
  order_priority_band: {order_id -> int}
  order_pre_score: {order_id -> float}
   planner_mode_cap: {order_id -> {A,B,C}}
   policy_mode_mask: {order_id -> {WAIT,B,C}}
  recovery_pool: {order_id -> [node_id]}
  node_charge_load_budget: {node_id -> int}
  route_drift_ref: {node_id -> (eta_ref, route_index_ref)}
  launch_candidate_stations: [node_id]
}
```

`launch_candidate_stations` 说明：

- 由 ALNS 基于订单分布粗筛，筛选规则为：卡车未来路线上的站点，且该站点周边 `support_radius_km` 内存在未分配订单
- 仅用于决定"卡车到达哪些站点时触发 `riding_with_truck` 无人机的 PPO 决策点"
- 不预算具体的回收候选点，回收候选点在 PPO 决策时实时计算（见 CMRAPPO 职责描述）
- `plan_version` 更新时，`launch_candidate_stations` 同步更新；已在飞行中的无人机不受影响

`node_charge_load_budget` 说明：

- `node_charge_load_budget` 保留在 `CoarsePlanView` 中
- 它只针对固定充换电节点：`station` / `depot`
- 它表示 coarse planner 对未来一段时间内节点**充换电服务阶段**承压的软预算
- 它不是物理工位容量，也不是 mode C 等待回收人数上限
- 其作用是给 `candidate_builder` / baseline / critic 提供固定节点拥挤先验，避免策略在 coarse 层面把过多 UAV 都导向同一个充换电节点
- 因此它仍然应该出现在当前 `CoarsePlanView` 契约中，但只能作为固定节点补能服务的规划侧引导信号，不能被解释为“等待回收容量”

PPO 的权限严格限定为：

1. 只能在 `authorized_orders` 内选订单
2. 只能在 `policy_mode_mask[order]` 内选模式（actor 侧）
3. `planner_mode_cap[order]` 仅作为上层语义边界，不直接作为 actor head 掩码
4. 当 `planner_mode_cap` 仅放行 `A` 时，该订单不进入本轮 PPO 可选订单集（或仅保留 `WAIT`）
5. `policy_mode_mask` 必须与 actor head 分支严格对齐为 `{WAIT,B,C}`
6. 只能在 `recovery_pool[order]` 内选回收节点
7. 不能改写 `truck_backbone_route`
8. 唯一例外：当所有 coarse-plan 授权动作都非法时，允许进入 `safety fallback action`（飞往 depot 或最近可达 station）

当 `plan_version` 更新时：

1. reservation 节点不再出现在 `truck_backbone_route` 中 → 立即失效
2. 订单不再属于 `authorized_orders` → 相关未执行局部动作立即失效
3. recovery 节点不再属于 `recovery_pool[order]` → 相关 `mode C` 动作立即失效
4. 已起飞且订单已送达的 UAV 不回滚订单状态，只重算返程
5. LSTM hidden state 不做全局硬重置，但观测中必须注入 `plan_version_delta`

### 4.1.2 需要完成的具体工作

1. 固定离线训练阶段主输入：
   - `scene_config.json`
   - `entities.json`
   - `orders.json`
   - `osm_network.xml/geojson`

2. 定义订单使用策略：
   - 卡车路径规划依据：`static_orders`（固定，第一阶段路径不优化）
   - PPO 训练订单源：泊松流（`arrival_rate > 0`，重量上限 ≤ 10kg）
   - `dynamic_orders` 训练阶段不使用，保留作为离线验证 benchmark

3. 定义统一订单源模式：
   - `order_source_mode = benchmark | poisson | hybrid`
   - 训练主循环只允许 `poisson`
   - `validate_benchmark.py` 只允许 `benchmark`
   - `validate_stochastic.py` 允许 `poisson` 或 `hybrid`
   - 泊松流复用 `backend/environment/state/order_manager.py` 的订单生成与 deadline 语义，不再单独重写一套离线规则

4. 定义训练专属配置文件：
   - `backend/config/rh_alns_cmrappo.yaml`
   - 配置文件必须按以下分层组织：`scene` / `data` / `planner` / `candidate` / `action_space` / `reservation` / `policy` / `reward` / `training` / `curriculum`

5. 定义模型元数据文件格式：
   - `backend/weights/rh_alns_cmrappo/<version>/meta.json`

6. 冻结环境语义契约（见 4.1.3 节）

### 4.1.3 必须先冻结的环境语义契约

离线训练与离线验证不能只依赖参数文件，还必须先冻结以下 7 件事：

**（1）模式 C 的合法回收动作**

模式 C 在离线环境中的正式语义：

- 无人机送达后，不允许飞向道路中途的运动中卡车
- "返回卡车"只能解释成返回卡车未来将经过的合法交接节点
- 合法交接节点仅限：`station` 或 `depot`

候选回收动作仅在同时满足以下条件时才能保留在 `action_mask` 中：

1. 该节点在卡车未来路径上，且仍未经过（`ETA_truck(r_j) > t_deliver`）
2. 无人机预计到达时刻早于卡车到达该节点（`ETA_uav(deliver -> r_j) + delta_safe <= ETA_truck(r_j)`）
3. 无人机剩余电量足以飞到该节点并保留安全余量（`E_need + E_safe <= E_rem`）

补充说明：

- UAV 在 `station` / `depot` 等待被卡车回收，本身不受充换电工位容量限制
- `parking_slots`、FIFO 队列和 `predicted_queue_time` 只约束**回收成功后的充换电服务阶段**
- 因此，站点充换电容量不应作为 mode C 等待回收动作的直接非法性条件

**（2）模式 C 返程的保底规则**

订单送达后，返程目标按以下顺序选择：

1. 卡车未来合法交接节点（mode C）
2. 仓库（depot）
3. 最近可达充换电站
4. 若上述均不可达，则进入硬失败

**（3）Reservation Timeout 机制（CCT 映射）**

本阶段必须实现 reservation timeout，这是 CCT 机制在本项目的正式映射：

- reservation 是"软预留、非排他、后续服务排队感知"的局部承诺，不是物理工位锁
- 每个 mode C 动作建立后，启动 timeout 计时器：

```text
tau_res = alpha * ETA_uav(deliver -> r_j)
        + beta  * t_hist(node_type_j, mode C)
        + gamma * q_est(r_j)
```

- timeout 判定条件（任一成立即失效）：

```text
timeout(res) =
    (t_now >= t_res_expire)
 OR (ETA_uav + delta_safe > ETA_truck)
 OR (energy_feasible == false)
 OR (route_drift_invalid == true)
 OR (node_available == false)
```

- timeout 后进入 `fallback_recovery`，施加局部软惩罚：

```text
r_timeout = - lambda_res_timeout * T_timeout_cost
```

**（4）软失败与硬失败**

软失败定义：

- 错过原定回收节点上的卡车，但仍能飞往某个合法安全节点
- 订单保持为已完成，失败的是返程回收子任务
- 软惩罚：`soft_miss_penalty = lambda_miss * T_fallback`

硬失败定义：

- 无人机当前不在 `station` / `truck` / `depot`
- 且剩余电量不足以飞往任何下一个合法充换电站或仓库
- 或已经出现半空中停电
- 记为 `airborne_energy_failure`，无人机退出当前 episode 可用集合

**（5）FIFO、工位容量与终端清场**

1. `parking_slots` 只决定**充换电服务**的并行上限
2. 超出服务容量后必须进入 FIFO 队列
3. `swap_time` / `charge_time` 结束后释放工位并唤醒队首
4. 排队时间按"入队时刻到开始服务时刻"精确累计
5. UAV 在节点等待被卡车回收，不属于充换电 FIFO 排队
6. episode 结束时必须清点：已完成订单、超时订单、仍在返程/排队/等待卡车/已硬失败的无人机

**（6）超时订单惩罚机制**

超时订单的惩罚必须采用实时累加方式，不允许仅在送达时结算：

- 每个仿真步，对所有未送达且已超时的订单，实时累加超时惩罚：
  ```text
  r_overdue_step = -lambda_overdue * sum_i max(0, t_now - deadline_i) * dt
  ```
- 送达超时订单时，停止该订单的累加，不再叠加额外惩罚（避免双重计入）
- 若订单超时时长超过 `max_overdue_sec` 仍未送达，强制从可调度池移除，记一次 `Lambda_hard_overdue` 惩罚

**为什么必须实时累加**：若仅在送达时结算，模型会学到"不送超时订单"可规避惩罚，
导致超时订单被系统性忽略。实时累加后，不送的代价持续增大，送达则停止累加，
模型始终有动力去送超时订单。

参数：
- `lambda_overdue = 0.20`（首轮建议值，高于 `lambda_wait`，体现超时的更高优先级）
- `max_overdue_sec = 600`（超过此阈值强制移除）
- `Lambda_hard_overdue = 600`（强制移除时的固定惩罚，秒级等价）

**（7）`riding_with_truck` 状态语义**

`riding_with_truck` 是无人机的一个正式状态，进入条件为：

- 系统启动时被预设为搭车出发的无人机，初始状态为 `riding_with_truck`
- 无人机通过 mode C 被卡车成功回收后，完成卡车上充换电，进入 `riding_with_truck`

该状态下的行为规则：

1. **决策触发**：卡车到达某个站点 `S_k`，且 `S_k ∈ CoarsePlanView.launch_candidate_stations`，则触发该卡车上所有 `riding_with_truck` 无人机的 PPO 决策点

**`launch_candidate_stations` 的选择与更新规则**

选择依据为订单密度，而非卡车路线形态。卡车路线本身已由 RH-ALNS 保证不走回头路、不大绕路，路线上的每个站点卡车都会经过，因此 `launch_candidate_stations` 只需在"卡车已确定会经过的站点"中，按订单密度筛选值得触发 PPO 决策的子集。

筛选规则（每次 ALNS 重规划时执行）：

```text
对卡车未来路线上的每个站点 S_k（卡车尚未经过）：
  如果 count(未分配订单 within support_radius_km of S_k) >= min_orders_to_trigger
  → 加入 launch_candidate_stations
```

参数：
- `support_radius_km = 1.2`（与现有候选集参数一致）
- `min_orders_to_trigger = 1`（首轮默认值；若训练中 WAIT 动作被选得过于频繁，可调至 2）

更新时机（三种）：

1. **ALNS 重规划时**（主要路径）：重新计算 `launch_candidate_stations`，与 `CoarsePlanView` 其他字段同步刷新，`plan_version` 递增
2. **卡车经过某站点后**：该站点自动从 `launch_candidate_stations` 中移除，无需等待 ALNS 重规划（轻量实时维护）
3. **某站点周边订单全部被分配后**：该站点从 `launch_candidate_stations` 中移除，避免无人机在此起飞后无订单可选只能 WAIT
2. **候选集实时构建**：对每个候选订单 `order_i`，实时计算：
   ```text
   t_deliver = t_now + fly_time(S_k → order_i.location)
   recovery_pool(order_i) = {r_j | 满足时序 + 电量约束，基于 t_deliver}
   ```
3. **动作空间与 idle 完全统一**：`(order_i, mode ∈ {WAIT/B/C}, recover_node_j)`
4. **WAIT 语义**：在 `riding_with_truck` 状态下，选择 WAIT = 继续搭车到下一站，不起飞
5. **不触发决策的站点**：若卡车到达的站点不在 `launch_candidate_stations` 中，无人机继续搭车，不触发 PPO 决策
6. **卡车上充换电**：无人机在卡车上充换电不占用站点 FIFO 队列，由 `truck_drone_recover_time_s` 决定服务时长；充换电完成后重新进入 `riding_with_truck` 状态
7. **取货**：起飞时直接从卡车取货，无需返回仓库

补充说明：

- 对 mode C 而言，`predicted_queue_time(node)` 可以作为“回收后若进入该节点充换电服务，预计会多拥挤”的软信号
- 它可以用于候选排序、soft penalty 估计、critic 特征或 heuristic 打分
- 但不应因为某节点后续充换电队列较长，就把“等待被卡车回收”这个动作直接判为非法

### 4.1.4 主目标函数（本阶段统一采用）

```text
J =
  T_complete
  + lambda_wait * T_wait
  + wait_idle_penalty_coef * T_idle
  + lambda_queue * T_queue
  + lambda_miss * T_fallback
  + lambda_res_timeout * T_res_timeout
  + lambda_overdue * T_overdue
  + Lambda_hard * N_hard_fail
  + Lambda_hard_overdue * N_hard_overdue
```

含义：

- `T_complete`：无人机可配送订单从进入可调度池到完成配送的时长总和，定义为
   `T_complete = sum_i (t_delivered_i - t_spawn_i)`，其中 `i` 仅遍历 `weight <= 10kg` 的无人机订单，
   `t_spawn_i` 是订单进入可调度池的时刻（对应 `spawn_sim_s`，静态订单取 `0`），
   `t_delivered_i` 是无人机完成配送的时刻（不含 `drone_service_time_order_s`，
   若有服务时长则取服务完成时刻，需与环境实现保持一致）。
   示例：3 个订单分别在进入池后耗时 10 分钟、20 分钟、40 分钟完成配送，则
   `T_complete = 70 分钟`。
   该目标只计入策略可影响的无人机订单时间段，避免把 `mode A` 卡车背景订单混入 PPO 主评估口径
- `T_wait`：UAV 在节点处被动等待卡车到来的物理等待时间总和，
   不包含显式 WAIT 动作产生的 idle 时间（两者必须分开累计，不允许重复计入）
- `T_idle`：UAV 显式选择 WAIT 动作后产生的 idle 时间总和，定义为
   `T_idle = sum_k delta_t_wait_k`（`delta_t_wait_k` 为第 k 次 WAIT 动作的仿真时间推进量）。
   即时惩罚 `r_wait_step = -wait_idle_penalty_coef * delta_t_wait_k` 在每步立即结算，
   不在 episode 末端累计，以保证梯度信号清晰。
   首轮建议 `wait_idle_penalty_coef = 0.10`，与 `lambda_wait` 同口径。
   排队（`T_queue`）、站点容量导致的被动等待不计入 `T_idle`
- `T_queue`：无人机在充换电站排队总时间
- `T_fallback`：错过卡车后飞往下一个站点或仓库的总时间
- `T_res_timeout`：reservation timeout 造成的总局部损失
- `T_overdue`：所有未送达且已超时订单的累计超时时长，定义为
   `T_overdue = sum_i max(0, t_now - deadline_i)`（对所有未送达订单，每步累加）。
   **必须在每个仿真步实时累加，不允许等到送达时才结算**，原因是：若仅在送达时结算，
   模型会学到"不送超时订单"可规避惩罚，导致超时订单被系统性忽略。
   实时累加后，送达超时订单会停止该订单的累加，模型仍有动力去送。
   送达时不再叠加额外惩罚，避免双重计入。
   首轮建议 `lambda_overdue = 0.20`（高于 `lambda_wait`，体现超时的更高优先级）
- `N_hard_overdue`：超时时长超过 `max_overdue_sec` 仍未送达的订单数。
   超过该阈值后订单从可调度池中强制移除，记一次固定惩罚 `Lambda_hard_overdue`，
   避免模型长期持有无法挽回的"死单"。
   首轮建议 `max_overdue_sec = 600`，`Lambda_hard_overdue = 600`
- `N_hard_fail`：硬失败次数

要求：

- `J` 仅作为评估指标，不直接用于 PPO 梯度更新
- benchmark 与 stochastic 验证必须回到这套 UAV-scope 指标，不允许只看 PPO reward
- `Lambda_hard` 首轮建议值为 `1200`（秒级等价损失），必须显著大于一次最坏可恢复扰动
- `T_complete` 仅用于评估，不计入训练 reward（见 4.1.4.1 节）
- `T_overdue` 的即时惩罚必须在 `env_adapter` 的 `compute_reward()` 里逐步结算，不能只在 episode 末端累计
- `T_idle` 的即时惩罚必须在 `env_adapter` 的 `compute_reward()` 里逐步结算
- `mode A` 背景订单不进入 `J` 与 PPO-vs-baseline 核心比较指标，仅进入单独的系统上下文统计区

### 4.1.4.1 PPO 训练用每步奖励 r_t

`J` 是 episode 级评估指标，PPO 实际用于梯度更新的是每步奖励 `r_t`。
`r_t` 是 `J` 各项的增量版本（去掉 `T_complete`），满足 `sum_t r_t ≈ -J_train`。

`T_complete` 不计入 `r_t`，原因是：送达时一次性扣 `(t_delivered - t_spawn)` 会使送达本身产生大额负奖励，
模型可能学到"慢送或不送"来规避这笔扣分。`T_overdue` 实时累加 + `R_delivery_bonus` 已足够驱动模型及时送达。

```text
r_t =

  [每步累加项，每个仿真步 dt 结算一次]
  - lambda_overdue * sum_i max(0, t_now - deadline_i) * dt   // 超时订单实时惩罚（仅无人机订单）
  - lambda_wait    * delta_wait_t                             // UAV 被动等待卡车
  - wait_idle_coef * delta_idle_t                             // UAV 主动选 WAIT 动作
  - lambda_queue   * delta_queue_t                            // 充换电站排队
  - lambda_miss    * delta_fallback_t                         // 错过卡车后飞往备用点

  [事件触发项，事件发生时一次性结算]
  + R_delivery_bonus                                          // 无人机成功送达一单（正奖励）
  - lambda_res_timeout * T_timeout_cost                       // reservation timeout
  - Lambda_hard                                               // 硬失败（半空停电等）
  - Lambda_hard_overdue                                       // 订单超时被强制移除
```

说明：

- `T_overdue` 只对无人机可配送订单（weight ≤ 10kg）累加；`mode A` 卡车背景订单无超时惩罚，也不进入 `T_complete` 与 PPO 主评估
- `R_delivery_bonus` 是唯一的正奖励项，确保模型始终有动力送达订单，即使订单已超时
- 首轮建议 `R_delivery_bonus = 60`（秒级等价，量级约为一次小惩罚）
- 所有每步累加项必须在 `env_adapter.compute_reward()` 中逐步结算，不允许在 episode 末端批量累计

### 4.1.5 参数分层要求

参数必须按以下分层组织，不允许混用：

- **共享运行时参数**（继续放在 `backend/config/drone_params.yaml`）：
  - 能耗模型、配送时长、操作时长、速度等物理参数

- **算法专属参数**（统一放在 `backend/config/rh_alns_cmrappo.yaml`）：
  - `scene`：地图尺度派生规则
  - `data`：订单源模式与数据注入规则（`order_source_mode`、`benchmark_use_dynamic_orders=true`、`poisson_arrival_rate`、`poisson_seed`、`poisson_weight_max=10kg`、`order_window_min_min=20`、`order_window_max_min=60`、`hybrid_background_dynamic_orders` 等）
  - `planner`：RH-ALNS 低频触发参数（`coarse_replan_interval_sec=420` 等）
  - `candidate`：候选集参数（`max_candidate_orders=32`、`max_candidate_recovery_per_order=4`、`max_candidate_actions=128`、`station_wait_threshold_sec=240` 等）
  - `action_space`：动作空间类型（`factorized`）、`enable_wait_action=true`、`include_mode_a_in_policy=false`
  - `reservation`：timeout 参数（`alpha=1.5`、`beta=1.2`、`gamma=0.3` 等）
  - `policy`：模型结构（`encoder_type=attn_lstm_lite`、`d_model=128`、`nhead=8`、`lstm_hidden=128`、`hist_len=6`、`critic_mode=centralized_train_only`、`inference_mode=greedy` 等）
  - `reward`：惩罚权重（`lambda_wait=0.10`、`wait_idle_penalty_coef=0.10`、`lambda_queue=0.10`、`lambda_miss=0.15`、`lambda_res_timeout=0.10`、`lambda_overdue=0.20`、`R_delivery_bonus=60`、`max_overdue_sec=600`、`hard_overdue_penalty_sec=600`、`hard_failure_penalty_sec=1200` 等）
  - `training`：PPO 超参数（`ppo_learning_rate=0.0003`、`rollout_steps=4096`、`batch_size=512` 等）
  - `curriculum`：课程学习噪声（首轮全部为 0）

### 4.1.6 产物

- `rh_alns_cmrappo.yaml` 初版（含完整分层结构）
- `meta.json` schema 初版
- 离线阶段参数清单
- 一份环境语义契约说明（可内嵌于 yaml 注释或单独文档）

### 4.1.7 验收标准

- 任何人只看配置文件就能知道训练输入和训练参数来源
- 不再混用旧共享配置与新算法专属权重/惩罚参数
- RH-ALNS 与 CMRAPPO 的职责边界已明确写入文档，`env_adapter` 的后续实现不会再对模式 C / miss / queue / reservation timeout 语义各自理解
- `CoarsePlanView` 接口契约已定义，后续实现可直接对照
- 主目标函数已包含 `lambda_res_timeout * T_res_timeout` 项，与算法方案文档一致

### 4.1.8 Phase 1 当前已完成内容（截至本轮）

本轮已完成以下 Phase 1 落地工作：

1. 已创建算法专属统一配置文件：
   - `backend/config/rh_alns_cmrappo.yaml`
   - 完成了 `scene / data / planner / candidate / action_space / reservation / policy / reward / training / curriculum` 的完整分层
   - 默认参数已按当前 `4×4km` 地图、`10` 个充换电站、`1` 辆卡车、`12` 架无人机的规模进行标定，并显式固化了训练与验证的复现参数（seed、order source mode、评估频率等）

2. 已创建训练期共享契约文件（`CoarsePlanView` 契约）：
   - `backend/training/contracts.py`
   - 已将文档中的 `CoarsePlanView` 正式收敛为代码级只读契约
   - 已定义 `RouteDriftRef`
   - 已定义相关基础类型与模式枚举：`NodeId`、`OrderId`、`PlannerMode`、`PolicyMode` 等
   - 已在契约层加入基础一致性校验，避免后续 `planner_bridge` 产出不完整或自相矛盾的 coarse plan

3. 已创建训练子包入口：
   - `backend/training/__init__.py`
   - 为后续 `scene_loader`、`order_source_adapter`、`planner_bridge`、`candidate_builder`、`env_adapter` 的实现预留统一包路径

4. 已在 `contracts.py` 中固化 `meta.json` schema（代码级契约）：
   - 新增 `MetaJson`：Phase 1 可冻结的结构参数契约，包含八个子契约：
     - `PolicyMeta`：模型结构参数（`d_model`、`nhead`、`lstm_hidden`、`hist_len`、`inference_mode` 等）
     - `ActionSpaceMeta`：动作空间定义（`factorized_head_order`、`policy_modes`、`planner_modes` 等）
     - `CandidateMeta`：候选集参数（`max_candidate_orders`、`max_candidate_actions`、安全余量等）
     - `PlannerMeta`：上层规划触发参数（`coarse_replan_interval_sec`、`support_radius_km` 等）
     - `RewardMeta`：奖惩权重（`lambda_*`、`R_delivery_bonus`、`hard_failure_penalty_sec` 等）
     - `SharedRuntimeParamsSnapshot`：共享运行时参数快照（对应 `backend/config/drone_params.yaml` 的物理参数、能耗参数、服务时长参数）
     - `EnvSemanticContractMeta`：环境语义契约摘要（mode C 回收节点类型、reservation timeout 开关、overdue 惩罚模式等）
     - `OnlineLockParams`：在线锁参数策略骨架（`locked_fields` / `tunable_fields`）
   - 新增 `TrainingRunMeta`：训练完成后才能填充的运行时字段（`model_version`、`trained_at`、`scene_id`、`scene_bundle_dir`、`training_input`）
   - 新增 `BenchmarkMeta`：benchmark 身份快照（`orders.json` 路径、整体摘要、`static_orders` / `dynamic_orders` 数量、`benchmark_use_dynamic_orders`）
   - 新增 `TrainingInputMeta`：训练输入快照（订单源模式、benchmark 身份、泊松参数、seed、总步数）
   - 新增 `build_meta_json_dict(meta, run)`：合并两部分生成完整 meta.json 内容
   - 设计原则：结构参数（Phase 1 冻结，`MetaJson`）与运行时字段（Phase 7 填充，`TrainingRunMeta`）分离，避免 `Optional` 字段弱化契约强度
   - 说明：更严格的 `meta.json` 完整性校验（例如训练主循环真实填充值校验、数值范围校验、写盘前终检）放到 Phase 7 的 `train_cmrappo.py` 中继续完善；Phase 1 先冻结字段结构与最小非空约束

5. 当前仍未完成、需要在后续继续推进的项：
   - `CoarsePlanView` 的实际生成逻辑（`planner_bridge.py`，Phase 6）
   - `CoarsePlanView` 消费侧的 `candidate_builder.py`（Phase 6）

说明：

- 本轮只完成了”契约与配置冻结”，尚未实现 `planner_bridge` 或任何真实 coarse planning 逻辑
- `contracts.py` 当前放在 `backend/training/` 下，是因为后续 Phase 2~Phase 7 的新模块都按文档规划落在该目录；若你后续希望将契约层上移到更通用的包路径（例如 `backend/core/contracts/`），建议尽早统一，避免后续导入路径扩散

后续 Phase 注意事项（与 meta.json 契约相关）：

- **Phase 2（scene_loader）**：`TrainingRunMeta.scene_id` 与 `scene_bundle_dir` 必须与 `scene_config.json` 中的实际值对齐；`scene_loader` 加载完成后应输出可直接填入 `TrainingRunMeta` 的字段值，不允许在 `train_cmrappo.py` 中再次硬编码场景路径
- **Phase 3（order_source_adapter）**：`TrainingInputMeta` 的所有字段必须从 `rh_alns_cmrappo.yaml` 的 `data` / `training` 节读取；`order_source_adapter` 应在构建订单源配置时同步输出可填入 `TrainingInputMeta` 的字段快照
- **Phase 7（train_cmrappo）**：训练完成时必须构造 `TrainingRunMeta`，调用 `build_meta_json_dict(MetaJson(...), TrainingRunMeta(...))` 序列化为 meta.json；`trained_at` 使用 ISO 8601 格式；不允许手写裸字典绕过契约；`MetaJson` 的各子契约字段值必须与本次训练实际使用的 yaml 参数严格一致，不允许复制粘贴旧版本值；并在写盘前补做完整性终检（benchmark 身份快照、共享运行时参数快照、scene_id / model_version / trained_at 等运行时字段齐全性）
- **Phase 11（模型产物固化）**：meta.json 由 `build_meta_json_dict` 生成后直接写入 `backend/weights/rh_alns_cmrappo/<version>/meta.json`；`benchmark_report.json` / `stochastic_report.json` 等报告文件路径在本阶段不进入 meta.json，但目录结构必须与 §4.11 保持一致，以便后续在线接入时直接定位

## 4.2 Phase 2：实现静态场景装载器

目标：

- 把 `default_scene` 的静态资产变成训练和验证都能直接读取的标准内部对象
- 只负责装载场景资产，不在这一阶段决定运行时采用哪种订单源模式

建议新增文件：

- `backend/training/scene_loader.py`

核心职责：

1. 读取 `scene_config.json`
2. 读取 `entities.json`
3. 读取 `orders.json`
4. 读取 `osm_network.xml/geojson`
5. 输出统一的静态场景上下文对象

建议输出结构：

- 场景边界
- depot / station / truck / drone 内部对象
- 固定静态订单集
- 固定动态订单集
- 路网引用或几何数据引用
- 标准化 `TrainingSceneContext`

实现要求：

- benchmark 订单必须支持按仿真秒回放
- 动态订单必须保留 `spawn_sim_s` / `deadline_offset_s`
- 所有实体字段名与当前项目一致，不引入第二套命名
- 场景对象中必须显式保留站点类型、容量、换电时长与卡车未来路径节点信息，供模式 C 和 FIFO 逻辑使用

产物：

- `scene_loader.py`
- `load_default_scene()` 入口
- 至少 1 个装载自测脚本

验收标准：

- 能打印默认场景摘要：
  - `1` depot
  - `10` stations
  - `1` truck
  - `12` drones
  - `10` static orders
  - `10` dynamic orders
- 多次加载结果一致，可复现

### 4.2.1 仓库现状核对（2026-04-24）

对当前仓库代码的核对结论如下。

**已经存在、可直接复用的能力**

- 场景原始资产读取已经存在：
  - `backend/environment/geo/preset_scenes.py:get_preset_scene()`
    可直接读取 `scene_config.json`、`entities.json`、`orders.json`、`osm_network.geojson`
  - `backend/environment/geo/preset_scenes.py:load_osm_from_cache()`
    可直接读取 `osm_network.xml` 与 `osm_network.geojson`
- 实体实例化已经存在：
  - `backend/environment/state/entity_manager.py:EntityManager.load_from_config()`
    已能把 `depots/stations/trucks/drones` 转成内部对象，包含坐标转换、类型分发与资产注册
- benchmark 动态单按仿真秒回放语义已经存在：
  - `backend/environment/state/order_manager.py:OrderManager.set_scheduled_dynamic_orders()`
  - `backend/environment/state/order_manager.py:OrderManager._order_from_scheduled_entry()`
    已支持 `spawn_sim_s`、`deadline_offset_s`、`deadline_sim_s`
- 泊松订单与 deadline 生成规则已经存在：
  - `backend/environment/state/order_manager.py:OrderManager._create_order()`
    与 `backend/config/rh_alns_cmrappo.yaml` 中
    `scene.order_window_min_min` / `scene.order_window_max_min` 的配置口径一致
- 训练侧元数据契约已经冻结一部分：
  - `backend/training/contracts.py:BenchmarkMeta`
  - `backend/training/contracts.py:TrainingInputMeta`
  - `backend/training/contracts.py:TrainingRunMeta`

**需要明确的边界**

- 当前仓库里还没有训练期入口把“场景文件读取 + 实体对象构建 + 静态订单/动态订单拆分 + 路网引用”组装成统一的 `TrainingSceneContext`
- `backend/environment/scene/scene_service.py:load_preset_scene()` 返回的是前端/场景服务用的 `SceneContext`
  - 它不返回训练期内部对象
  - 不产出 `Depot / SwapStation / Truck / Drone / Order` 这套训练期结构
  - 还会重新生成随机 `scene_id`
  - 因此不能直接代替 `scene_loader`
- 当前训练包 `backend/training/` 中只有契约层文件，尚不存在：
  - `scene_loader.py`
  - `TrainingSceneContext`
  - `load_default_scene()` 训练入口
- 静态订单目前没有训练侧标准装载器
  - 仓库中确实已有在线仿真入口 `backend/api/routes/simulation_bp.py:_load_initial_orders()`
    可把订单字典转成 `Order`
  - 但它属于 API 初始化胶水逻辑，不是训练模块，也不会输出标准化训练场景上下文
- “卡车未来路径节点信息”当前也不是现成的场景装载产物
  - `backend/core/entities/truck.py` 只提供运行时 `route_nodes` 容器
  - `backend/solver/greedy_mmce.py:TruckRoute` 与 `_build_truck_route()` 属于求解器运行时构建逻辑
  - `backend/solver/decision_engine.py` 在应用调度结果时才调用 `truck.set_route()`
  - 因此，文档要求的 `truck_backbone_route` / 未来路径节点信息，当前不能通过静态场景读取直接得到

**对 Phase 2 的评估结论**

- “已有若干可复用片段，但还不能直接满足 Phase 2” 这一判断是正确的
- Phase 2 当前缺的不是底层能力，而是训练侧编排层：
  - 统一场景装载入口
  - 标准化 `TrainingSceneContext`
  - 静态订单/动态订单的训练期拆分与挂接
  - 路网资产引用统一输出
- 需特别注意 `Phase 2` 与 `Phase 4` 的边界：
  - `Phase 2` 至少要保留生成卡车骨架路线所需的静态资产与字段
  - 完整的 `truck_backbone_route`、ETA 与 SUMO 校验产物更接近 `Phase 4` 的职责
  - 实现时不应把这两阶段混成一个“只靠读取 JSON 就自动得到完整卡车未来路径”的错误预期

## 4.3 Phase 3：实现订单源适配（`benchmark` / `poisson` / `hybrid`）

目标：

- 把“场景资产”与“运行时订单注入模式”解耦
- 明确训练、benchmark 验证、stochastic 验证分别使用哪一种订单源
- 复用现有 `OrderManager` 的泊松生成与 deadline 逻辑，不单独复制实现

建议新增文件：

- `backend/training/order_source_adapter.py`

核心职责：

1. 定义统一模式：
   - `benchmark`：`static_orders + dynamic_orders`，并强制 `arrival_rate = 0`
     其中 `dynamic_orders` 作为 PPO 主验证订单，`static_orders` 作为卡车骨架与 `mode A` 背景任务
   - `poisson`：`static_orders` 保留为卡车骨架/`mode A` 背景订单，动态订单仅由泊松流生成，不注入 `dynamic_orders`
   - `hybrid`：`static_orders + dynamic_orders + 泊松流`
2. 统一输出运行时订单源配置：
   - `scheduled_dynamic_orders`
   - `poisson_gen_config`
   - `arrival_rate`
   - `seed`
   - deadline/window 参数快照
3. 复用 `backend/environment/state/order_manager.py` 现有规则：
   - 泊松到达过程复用 `random.expovariate(arrival_rate / 60.0)`
   - 泊松订单 deadline 复用 `_create_order()` 的 `window_min/window_max`
   - benchmark 动态订单回放复用 `_order_from_scheduled_entry()` 的 `spawn_sim_s / deadline_sim_s / deadline_offset_s`
4. 提供统一入口：
   - `build_order_source(scene_ctx, mode, seed, overrides)`
5. 明确模式约束：
   - `train_cmrappo.py` 仅允许 `poisson`
   - `validate_benchmark.py` 仅允许 `benchmark`
   - `validate_stochastic.py` 仅允许 `poisson` 或 `hybrid`

实现要求：

- 同一 `seed` 下，泊松订单流必须可复现
- `poisson` 模式下不得把 `dynamic_orders` 当作背景事件偷偷注入
- `benchmark` 模式下必须显式关闭泊松流（`arrival_rate = 0`）
- 所有订单源配置必须写入训练配置快照和 `meta.json`

产物：

- `order_source_adapter.py`
- 订单源模式说明
- 至少 1 个自测脚本：分别打印三种模式下的订单源摘要

验收标准：

- 三种模式的输入边界明确且互不混淆
- `poisson` 模式能稳定复现订单数、seed 与 deadline 分布
- `benchmark` 模式下结果只受固定订单回放影响，不受泊松流扰动

## 4.4 Phase 4：SUMO 卡车路径验证（env_adapter 前置）

目标：

- 在实现 env_adapter 之前，先用 SUMO 验证卡车路径规划的正确性
- 确认 ETA、经过节点、mode C 合法回收节点集合在物理上是可信的

**为什么必须在 env_adapter 之前做**

卡车路径决定了 env_adapter 里三件核心事：

1. mode C 的合法回收节点集合（`recovery_pool`）
2. 各 station/depot 的 `ETA_truck`（直接影响 `action_mask`）
3. `CoarsePlanView.truck_backbone_route` 的内容

如果路径有问题，训练时 `action_mask` 会基于错误的 ETA 构建，模型学到错误的 mode C 行为，且无法区分是模型问题还是路径问题。

建议新增文件：

- `backend/training/export_sumo_truck_route.py`

职责：

1. 读取 `static_orders` 和 `osm_network.xml`，生成卡车骨架路线
2. 把卡车路线、depot/station 坐标导出为 SUMO 可加载文件（`poi.add.xml`、路线文件）
3. 在 `sumo-gui` 中验证：
   - 卡车路线是否连通
   - 是否经过预期的 station/depot 节点
   - 各节点的 ETA 是否符合物理约束（`truck.speed = 15 m/s`）
   - 路线覆盖的地理范围是否与 `scene_config.json.bounds` 一致

产物：

- `poi.add.xml`（depot/station 坐标）
- 卡车路线导出文件
- 各节点 ETA 列表（供 env_adapter 直接使用，作为 `CoarsePlanView` 的初始输入）

验收标准：

- `sumo-gui` 能正确加载路线和设施
- 卡车路线经过所有预期 station/depot
- ETA 与手工估算（距离 / 速度）误差在合理范围内
- 确认至少有 2 个以上合法 mode C 回收节点存在于路线中

## 4.5 Phase 5：实现训练环境适配器

目标：

- 把当前场景和调度问题封装成可训练环境

建议新增文件：

- `backend/training/env_adapter.py`

最小职责：

- `reset()`
- `step(action)`
- `advance_to_decision_event()`
- `build_runtime_state_view()`
- `compute_reward()`
- `is_done()`

职责边界（必须固定）：

- `env_adapter` 是唯一的运行时真相源，负责事件推进、状态转移、奖励结算、终止判定
- `env_adapter` 不再单独重实现一套候选动作筛选或 `action_mask` 逻辑
- 当环境推进到决策事件时：
  1. `env_adapter` 调用 `planner_bridge.maybe_replan(...)` 刷新 `CoarsePlanView`
  2. `env_adapter` 调用 `candidate_builder.build(...)` 生成 `observation + action_mask + action_lookup`
  3. policy 或 baseline 在该上下文上选动作
  4. `env_adapter.step(action)` 执行真实状态转移并结算 reward

第一版环境建议：

- 场景固定为 `default_scene`
- 运行时订单源通过 `order_source_adapter` 注入
- 训练阶段默认 `order_source_mode = poisson`
- benchmark 验证阶段默认 `order_source_mode = benchmark`
- stochastic 验证阶段默认 `order_source_mode = poisson | hybrid`
- 每个 `step` 对应一次调度决策窗口
- 不要求一开始就是全精度连续物理仿真，但必须优先保证事件状态转移正确
- 环境推进建议采用事件驱动或准事件驱动，而不是粗时间步近似

需要先定清楚：

1. 观测包含什么
2. 动作代表什么（factorized：order → mode → recover_node）
3. 奖励在什么时刻结算
4. 终止条件是什么

   episode 终止条件（任一满足即触发 `is_done() = True`）：
   ```text
   1. t_now >= upper_horizon_sec（默认 3600s，仿真时间上限）
   2. 所有订单已送达或已被强制移除（pending + assigned 均为空，N_hard_overdue 覆盖超时死单）
   3. 所有无人机均处于 airborne_energy_failure 状态（无可用无人机）
   ```
   说明：条件 1 是主要终止路径；条件 2 是提前终止（所有任务完成）；
   条件 3 是灾难性终止，应触发大额惩罚后结束。
   三者均不满足时 episode 继续推进。

这一阶段必须明确实现以下状态和事件：

1. 无人机状态
   - `idle`
   - `flying_to_deliver`
   - `delivered`
   - `return_to_rendezvous`
   - `return_to_station`
   - `return_to_depot`
   - `queueing_at_station`
   - `charging_or_swap`
   - `fallback_recovery`
   - `recovered`
   - `airborne_energy_failure`
   - `riding_with_truck`（新增：无人机在卡车上，含初始搭车出发和 mode C 回收后）

2. 关键事件
   - 订单送达
   - truck-side `mode A` 背景订单完成
   - 返程回收点选择（factorized action）
   - reservation 建立与 timeout 检查
   - 卡车到站
   - 无人机到站
   - 错过卡车（触发 fallback_recovery）
   - FIFO 入队 / 出队
   - 半空中能源硬失败
   - 卡车到达 `launch_candidate_station`（新增：触发 `riding_with_truck` 无人机的 PPO 决策点）
   - 无人机在卡车上完成充换电（新增：`charging_on_truck` → `riding_with_truck`）
   - 无人机从卡车起飞（新增：`riding_with_truck` → `flying_to_deliver`）

3. 奖励与惩罚来源（必须与主目标逐项对应）
   - `T_complete`（仅无人机订单）
   - `T_wait`（UAV 被动等待卡车的物理等待时间，episode 级累计）
   - `T_idle`（UAV 显式选择 WAIT 动作的 idle 时间，**每步即时结算**，不在 episode 末端累计）
   - `T_queue`
   - `T_fallback`
   - `T_res_timeout`（reservation timeout 造成的局部损失）
   - `T_overdue`（未送达超时订单的累计超时时长，**每步即时结算**，不在 episode 末端累计）
   - `N_hard_overdue`（超时超过 `max_overdue_sec` 被强制移除的订单数，一次性固定惩罚）
   - `N_hard_fail`

4. 局部承诺状态（reservation）
   - 当前预留的 `recover_node`
   - reservation 发起时刻与过期时刻
   - `reservation_count(node)` 作为拥挤信号
   - timeout 后自动进入 `fallback_recovery` 并施加软惩罚

5. 系统上下文统计（不进入 PPO 主指标）
   - `mode_a_background_order_count`
   - `mode_a_background_completion_time_sum`
   - `truck_total_mileage`
   - `truck_background_order_completion_events`

动作建议：

- 采用 factorized action space：先选订单，再选模式（`WAIT`/`B`/`C`），再选回收节点
- `WAIT` 为必备动作，不允许被 mask 掉
- `mode A` 不进入 PPO 动作空间
- 全程配合 `action_mask`

产物：

- `env_adapter.py`
- 一个最小可运行 episode
- runtime state / reward / done 说明

验收标准：

- `reset -> step -> ... -> done` 能完整跑通
- 不出现非法动作、空 mask、订单状态错乱
- 错过卡车时订单不会被错误回滚
- 硬失败时无人机会被正确移出后续可用集合
- 站点排队和工位释放逻辑可复现
- reservation timeout 能正确触发 fallback 并记录 `T_res_timeout`

## 4.6 Phase 6：实现候选动作与上层规划骨架

目标：

- 先把训练真正依赖的动作空间稳定下来
- 实现固定 RH-ALNS 骨架的简化版本，输出 `CoarsePlanView`

建议新增文件：

- `backend/training/candidate_builder.py`
- `backend/training/planner_bridge.py`

职责拆分：

1. `candidate_builder.py`
   - 只读 `env_adapter` 当前运行时状态与 `CoarsePlanView`
   - 输出 policy/baseline 直接消费的 `observation + action_mask + action_lookup`
   - 构建候选订单（在 `authorized_orders` 范围内）
   - 构建候选回收点（在 `recovery_pool[order]` 范围内）
   - 构建 factorized action mask（order → mode → recover_node 三阶段）
   - 实现 `max_candidate_actions` 截断逻辑（先按 `order_pre_score` 截断低优先级订单，再截断较差 recovery 候选，不允许截掉 `WAIT`）
   - **新增**：支持 `riding_with_truck` 触发的候选集构建：
     - 输入：当前站点 `S_k`、当前时刻 `t_now`、卡车剩余路线与 ETA
     - 对每个候选订单实时计算 `t_deliver = t_now + fly_time(S_k → order_i.location)`
     - 基于实时 `t_deliver` 计算合法 `recovery_pool(order_i)`
     - 构建与 `idle` 状态完全一致的 factorized action mask

2. `planner_bridge.py`
   - 低频读取全局运行时状态，生成或刷新只读 `CoarsePlanView`
   - 承接 RH-ALNS 上层骨架，输出 `CoarsePlanView`
   - 管理重规划周期（`coarse_replan_interval_sec=420`）
   - 管理 `plan_version` 更新与失效规则
   - 不直接推进环境事件，不直接结算奖励
   - 本阶段 ALNS 可以简化实现（如基于贪心或固定规则），但接口必须满足 `CoarsePlanView` 契约
   - **新增**：输出 `launch_candidate_stations`，筛选规则为：
     - 卡车未来路线上尚未经过的站点，且该站点周边 `support_radius_km` 内存在至少 `min_orders_to_trigger` 个未分配订单
     - 卡车路线约束（不绕路）已在路线规划阶段保证，此处只做订单密度筛选
     - 更新时机：ALNS 重规划时全量重算；卡车经过某站点后实时移除；某站点周边订单全部被分配后实时移除
     - 首轮参数：`min_orders_to_trigger = 1`，可通过调高该值减少触发频率

固定调用链：

1. `env_adapter.advance_to_decision_event()`
2. `planner_bridge.maybe_replan(runtime_state) -> CoarsePlanView`
3. `candidate_builder.build(runtime_state, coarse_plan) -> observation + action_mask + action_lookup`
4. `policy` 或 `baseline` 选择动作
5. `env_adapter.step(action)` 执行真实状态转移并结算 reward

这一阶段必须先固定以下参数：

- `support_radius_km`（默认 `1.2`）
- `max_candidate_orders`（默认 `32`）
- `max_candidate_recovery_per_order`（默认 `4`）
- `max_candidate_actions`（默认 `128`）
- `station_wait_threshold_sec`（默认 `240`）
- `coarse_replan_interval_sec`（默认 `420`）
- `coarse_new_order_trigger`（默认 `3`）
- `upper_horizon_sec`（默认 `3600`）

原因：

- 这些参数直接定义动作空间
- 动作空间变了，训练权重的语义也会变

同时必须把以下语义写进候选动作生成器：

1. 模式 C 候选回收点只能来自卡车未来合法交接节点或保底合法节点
2. 若 `ETA_uav + delta_safe > ETA_truck`，该回收动作必须直接裁掉
3. 若电量不足以到达候选回收点并保留安全余量，该动作必须直接裁掉
4. `predicted_queue_time(node)` 可作为排序或软截断信号，但不应单独作为 mode C 等待回收动作的硬性非法判据
5. 若模式 C 原候选失效，fallback 只能指向最近可达充换电站或仓库
6. `WAIT` 必须始终存在于模式阶段动作域中，不允许被 mask 掉

产物：

- 候选动作生成器（factorized）
- observation / action mask / action lookup 生成器（三阶段）
- 上层规划骨架入口（`CoarsePlanView` 输出）

验收标准：

- 在 `default_scene` 上每一步都能稳定生成候选集
- `max_candidate_actions` 不溢出
- mask 与候选动作一一对应
- `CoarsePlanView` 输出满足接口契约，PPO 可直接消费

## 4.7 Phase 7：实现 PPO 训练主循环

目标：

- 让环境和模型真正连起来，产出第一版权重

建议新增文件：

- `backend/training/train_cmrappo.py`
- `backend/training/model.py`
- `backend/training/rollout_buffer.py`

最小训练闭环需要：

- 模型前向（factorized actor head + centralized critic head）
- action mask 采样（三阶段 factorized）
- rollout 收集
- advantage 计算（GAE，`gae_lambda=0.95`）
- PPO 更新（`clip_coef=0.2`）
- checkpoint 保存

**模型结构（Shared PPO-Lite）**

建议结构：

1. Task encoder：对候选订单集合编码
2. UAV / station / truck encoder：对局部载体与基础设施上下文编码
3. Cross-attention：融合"当前决策无人机"和"候选订单/回收节点"关系
4. LSTM temporal unit：建模最近 `hist_len=6` 步局部决策历史（6 个"局部决策事件步"，不是固定仿真秒步）
5. Actor head：输出 factorized masked action logits（三阶段）
6. Critic head：训练时使用 centralized critic（可额外读取全局订单池、UAV 可用性、station queue、coarse-plan 全局摘要）

关键参数：

- `d_model=128`、`nhead=8`、`ff_dim=256`、`lstm_hidden=128`、`lstm_layers=1`
- `critic_mode=centralized_train_only`（在线推理不需要 centralized critic）
- `inference_mode=greedy`（推理阶段默认 greedy，不引入额外随机性）

**观测张量规范（5 类 token）**

1. `uav_self_token`：当前决策 UAV 的单个 token（位置、剩余电量、状态、剩余时间、reservation 状态、`plan_version_delta`、`is_riding_truck`、`drone_source_type`）
   - `is_riding_truck: bool`：是否处于 `riding_with_truck` 状态，用于区分两类决策触发
   - `drone_source_type: {depot, truck}`：当前无人机的取货来源类型（从仓库出发 or 从卡车起飞）
2. `order_tokens`：最多 `max_candidate_orders=32` 个候选订单 token
3. `recovery_tokens`：对当前所选订单的回收节点候选（最多 `max_candidate_recovery_per_order=4` 个）
4. `infra_tokens`：truck 骨架摘要 + station/depot 摘要（queue、ETA、parking_slots、node_charge_load_budget）
   - 其中 `queue` / `parking_slots` 描述充换电服务阶段负载
   - `node_charge_load_budget` 描述固定节点充换电服务的 planner 侧软预算，不表示等待回收容量
5. `history_tokens`：最近 `hist_len=6` 个局部决策事件摘要

Padding/Masking 规则：

- `padding_mask`：只用于 attention 层屏蔽无效 token
- `action_mask`：只用于 actor head 屏蔽物理非法动作
- 两者不允许混用

训练时必须保证：

- reward 与第 4.1.4 节主目标逐项对应（含 `T_res_timeout`）
- 软失败通过 `T_fallback` 进入小惩罚项
- reservation timeout 通过 `T_res_timeout` 进入小惩罚项
- 硬失败通过 `N_hard_fail` 进入大惩罚项（`Lambda_hard=1200`）
- 不允许继续使用与主目标脱节的混杂代理奖励作为默认训练目标

训练建议分两阶段：

1. 固定 seed 的小规模 `poisson` 过拟合阶段
   - 目的：确认模型、奖励、动作空间、订单源接入没有定义错

2. 多 seed `poisson` 扩展阶段
   - 目的：逐步获得对泊松动态订单的泛化能力

订单源要求：

- `train_cmrappo.py` 运行时强制校验 `order_source_mode == poisson`
- 训练配置快照必须记录 `arrival_rate`、`seed`、deadline 窗口参数
- 写入 `meta.json` 时必须同时记录 benchmark 身份快照（当前建议使用 `orders.json` 路径/摘要 + `static_orders` / `dynamic_orders` 数量），并与泊松参数、随机种子一起构成订单源确定性描述
- 若用户误传 `benchmark` 或 `hybrid` 到训练主循环，应直接报错，避免训练分布漂移

训练输出必须包含：

- `policy.pt`
- `meta.json`
- `train_metrics.jsonl` 或等价训练日志
- 训练配置快照

验收标准：

- 训练前几轮不会直接发散或崩溃
- 能稳定保存权重和元数据
- 固定 benchmark 的 UAV-scope 回放指标出现提升趋势
- reward 提升时，主目标分解项（含 `T_res_timeout`）也能同步解释，而不是只表现为黑盒数值变化

## 4.8 Phase 8：固定 benchmark 离线验证

目标：

- 先做确定性验证，不先做随机场景

建议新增文件：

- `backend/training/validate_benchmark.py`
- `backend/training/baselines/greedy_local_policy.py`

输入：

- `default_scene/entities.json`
- `default_scene/orders.json`
- `policy.pt`
- `meta.json`

验证方式：

- 固定 `static_orders + dynamic_orders`
- 关闭随机泊松流：`arrival_rate = 0`
- 多次重复跑同一组场景
- 与确定性 greedy baseline 对比
- PPO 与 baseline 的核心比较范围仅限无人机订单；`mode A` 背景订单只作为系统上下文统计保留

baseline 设计（本阶段正式采用）：

1. baseline 与 PPO 共用完全相同的：
   - `env_adapter`
   - `planner_bridge`
   - `candidate_builder`
   - `CoarsePlanView`
   - factorized action space 与 `action_mask`
2. baseline 为确定性策略，不做训练，不引入额外随机性
3. 候选订单选择规则采用加权贪心分数：
   ```text
   score(order_i) =
       w_priority * order_pre_score_i
     - w_eta      * eta_to_deliver_i
     - w_queue    * predicted_queue_time(best_recovery_i)
     - w_risk     * miss_risk_i
     - w_overdue  * projected_overdue_cost_i
   ```
4. 模式选择规则：
   - 若存在满足安全时序与电量约束的 `mode C`，且其后续服务拥挤代价可接受、`ETA_margin >= greedy_c_margin_min_sec`，优先选 `C`
   - 否则选 `B`
   - 若无合法配送动作，才选 `WAIT`
5. 回收点选择规则：
   - 在合法候选中选择 `ETA_margin` 更大、后续服务拥挤代价更小、额外 detour 更低的点
6. `validate_benchmark.py` 必须支持：
   - `policy_source=ppo`
   - `policy_source=greedy_baseline`

建议至少统计这些指标：

- 无人机订单完成数
- 无人机订单准时率
- 无人机订单超时数
- UAV-scope 主目标值（`J`）及各分解项
- 无人机总里程
- 回收站平均等待
- 模式分布（mode B / C 采用比例）
- 每单平均响应时延（仅无人机订单）
- 无人机等待卡车时间（`T_wait`）
- 站点排队总时间（`T_queue`）
- miss 次数
- fallback 总时间（`T_fallback`）
- reservation timeout 次数（`T_res_timeout`）
- hard failure 次数（`N_hard_fail`）
- 超时订单累计惩罚（`T_overdue`）
- 强制移除订单数（`N_hard_overdue`）
- greedy 推理下的吞吐与稳定性指标

系统上下文统计区至少保留：

- `mode_a_background_order_count`
- `mode_a_background_completion_time_sum`
- `truck_total_mileage`
- `truck_background_order_completion_events`

这一阶段最重要的是：

- 可复现性
- 可解释性
- 公平对比（同一环境、同一 coarse plan、同一 action mask）

产物：

- `benchmark_report.json`
- `benchmark_summary.md`
- baseline 对比结果表
- 系统上下文统计区

验收标准：

- 同一权重、同一输入，多次运行结果一致或波动极小
- PPO 与 greedy baseline 在同一设置下可直接对比
- 至少有一项 UAV-scope 核心指标优于 greedy baseline，或策略行为明显更合理
- 至少能解释模式 C 回收成功率、miss 率与 hard failure 率

## 4.9 Phase 9：随机扩展离线验证

目标：

- 在固定 benchmark 通过后，再验证对泊松动态订单的泛化

建议新增文件：

- `backend/training/validate_stochastic.py`

验证方式：

- 保持 `default_scene` 的设施和地图不变
- 订单源模式限定为：
  - `poisson`
  - `hybrid`
- `validate_stochastic.py` 不允许 `benchmark` 模式
- 按多个随机 seed 评估

建议分档：

- 低强度：`arrival_rate = 0.2`
- 中强度：`arrival_rate = 0.4`
- 高强度：`arrival_rate = 0.6`

重点观察：

- 训练分布内表现是否稳定
- 是否出现明显退化
- 候选集是否经常截断
- action mask 是否失衡
- miss / fallback / hard failure 是否随流量上升而失控
- FIFO 队列长度与等待时间是否出现异常爆炸

产物：

- 随机验证报告
- 不同强度分档结果表
- seed 汇总统计

验收标准：

- 中强度场景下结果稳定
- 高强度下即使性能下降，也不出现明显策略崩坏

## 4.10 Phase 10：SUMO 调度行为回放验证（训练后）

目标：

- 把训练好的模型的调度行为导出到 SUMO，验证 mode C rendezvous、miss、fallback 在时间轴上是否可解释
- 与 Phase 4 的路径验证不同，这里验证的是策略行为，而不是路径本身

建议新增文件：

- `backend/training/export_sumo_replay.py`

职责：

- 把离线验证（Phase 8）产生的路线、事件、订单结果导出成 SUMO 可观察文件

这一阶段主要看：

- 调度行为是否出现明显不合理绕行或聚集
- 模式 C 的 rendezvous、miss 与 fallback 行为是否在时间轴上可解释
- 是否出现明显违反站点容量或空中失效后仍继续派单的错误

产物：

- 回放导出文件
- SUMO 运行说明

验收标准：

- `sumo-gui` 能正确加载
- 至少完成 1 个 benchmark 回放可视化

## 4.11 Phase 11：模型产物固化

每个训练版本建议固化为如下目录：

```text
backend/weights/rh_alns_cmrappo/<version>/
  policy.pt
  meta.json
  benchmark_report.json
  stochastic_report.json
  baseline_benchmark_report.json
  system_context_report.json
  train_metrics.jsonl
  sumo/
    poi.add.xml
    replay.*
```

`meta.json` 至少包含：

- 模型结构参数
- 候选集参数
- 上层规划参数（含 `CoarsePlanView` 接口版本）
- 奖励/惩罚参数（含 `lambda_res_timeout` 和 `hard_failure_penalty_sec`）
- 共享运行时参数快照
- 训练所用场景 ID
- 训练所用 benchmark 身份快照（当前建议记录 `orders.json` 路径/摘要、`static_orders` / `dynamic_orders` 数量；再结合泊松参数与随机种子确定订单源）
- 后续在线锁参数策略骨架
- 当前版本冻结的环境语义契约摘要（含 reservation timeout 机制版本）

说明：

- 虽然当前阶段还不接前后端
- 但 `meta.json` 必须在本阶段就做，因为它是下一阶段线上锁参数的基础

---

## 5. 当前最合适的实际开工顺序

建议按以下小顺序开工：

1. 先做 `backend/config/rh_alns_cmrappo.yaml`（含完整分层结构）
2. 再冻结第 4.1.3 节的环境语义契约（含 reservation timeout 机制）
3. 再做 `backend/training/scene_loader.py`（只负责静态场景资产装载）
4. 再做 `backend/training/order_source_adapter.py`（统一 `benchmark / poisson / hybrid` 三种模式）
5. 再做 `backend/training/export_sumo_truck_route.py`，用 SUMO 验证卡车路径和 ETA
6. 再做 `backend/training/env_adapter.py`（含 reservation timeout 状态与事件，ETA 直接使用 Phase 4 产物）
7. 再做 `backend/training/candidate_builder.py` 和 `planner_bridge.py`（固定 RH-ALNS 骨架，输出 `CoarsePlanView`）
8. 再做 `backend/training/train_cmrappo.py`（Shared PPO-Lite，`order_source_mode=poisson`，factorized action space，centralized_train_only critic）
9. 训练出第一版 `policy.pt + meta.json`
10. 做 `backend/training/validate_benchmark.py` 与 `backend/training/baselines/greedy_local_policy.py`（固定 benchmark，对比 PPO 与 greedy baseline）
11. 再做 `backend/training/validate_stochastic.py`
12. 最后做 `backend/training/export_sumo_replay.py`（调度行为回放）

原因：

- 订单源模式必须先固定，否则训练、benchmark、stochastic 三条链路会混用订单注入语义
- SUMO 路径验证（步骤 5）必须在 env_adapter 之前，因为 ETA 是 `action_mask` 的前置输入
- 先把路径正确性确认，再实现环境，避免训练时无法区分路径 bug 和模型 bug
- baseline 对比放在 benchmark 阶段最合适，因为它要求完全相同的环境与动作空间
- 随机泊松流验证和行为回放放在模型能工作之后，避免同时面对多类 bug

---

## 6. 本阶段完成判断标准

离线训练与离线验证阶段完成，至少要满足：

1. 能稳定产出 `policy.pt + meta.json`
2. 能在 `default_scene` 上完成固定 benchmark 回放（greedy inference）
3. 能输出 UAV-scope benchmark 指标报告（含 `T_res_timeout`、`T_overdue`、`N_hard_overdue`、reservation timeout 次数）
4. 能做至少一组随机泊松扩展验证
5. 能导出 SUMO 可视化文件并复核结果
6. 已明确哪些参数属于后续在线必须锁定
7. 模式 C、miss、fallback、reservation timeout、FIFO 与 hard failure 语义已在环境和验证中闭环
8. RH-ALNS 与 CMRAPPO 的职责边界在代码层面已明确分离（`planner_bridge.py` 与 `env_adapter.py` 各司其职）
9. `mode A` 背景订单已从 PPO 主指标中剥离，并单独输出系统上下文统计（订单数、完成时间、卡车里程）

---

## 7. 下一阶段接口预留

虽然本阶段不接现有前后端，但要提前保留以下接口能力：

1. `meta.json` 中保留参数锁定策略字段
2. 训练输出目录结构固定化
3. solver 后续可直接加载：
   - `policy.pt`
   - `meta.json`
4. benchmark / stochastic / SUMO 报告文件命名固定化

这样下一阶段做在线推理接入时，不需要回头重构训练产物格式。
