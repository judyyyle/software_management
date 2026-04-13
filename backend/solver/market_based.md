# 市场拍卖调度 — 代码实现说明

本文档描述 `backend/solver/market_based_solver.py` 的**当前实现**，与仓库内其他设计/公式文档对照阅读。实现类 `MarketBasedSolver` **继承** `GreedyBaseline`，复用 OSM 路网加载、`_build_truck_route`、`_score_allocation`、`DispatchPlan` / `AllocationResult` 及 `decision_engine` 执行链。

**设计原则（动态拍卖约束协议）**：**状态决定权限，执行即锁定，路径必闭环**。

---

## 1. 适用范围与调度入口

| 入口 | 作用 |
|------|------|
| `dispatch(pending_orders, current_time, bbox, scene_id)` | 全量批次：处理本轮传入的全部待分配订单。 |
| `dispatch_incremental(new_orders, current_time, bbox, scene_id)` | 增量：仅对过滤后的新单跑一轮贪心 Phase 1–3；**尊重**求解器内已存在的 `RendezvousContract`（见下）。`summary["dispatch_type"] = "incremental"`。 |

**说明**：`dispatch_incremental` 不会自动「冻结仿真」；调用方需在适当时机暂停业务逻辑后再调用。契约列表 `_active_contracts` 挂在 **solver 实例**上，跨多次 `dispatch` / `dispatch_incremental` 保留，直至过期、由 `fulfill_contract` 兑现或被弹性归巢释放。

**增量与不可变更订单**：增量入口会先过滤掉已进入履约临界点的订单（见 §4），`summary["auction_stats"]["immutable_orders_skipped"]` 记录本轮跳过的单数；这些订单不再进入拍卖池，避免与「唯一履约权」冲突。

**仿真层接入**：`DispatchDecisionEngine` 提供 `execute_incremental(new_orders, current_time, bbox, scene_id)` 方法，自动调用 `dispatch_incremental` 并应用分配方案。`try_fulfill_contracts(current_time)` 可在每个仿真 tick 中调用，自动兑现已完成回收的契约。

**实体层与订单池**：`EntityManager.tick_all(current_time, dt, order_mgr)` 由 `SimulationEngine` 传入 `OrderManager`，仅用于配送完成时的订单归档；实体容器不再持有 `order_mgr` 字段。市场求解器对订单状态的读取见上文 `bind_order_manager`。

**总体能耗/成本汇总不变**：`_recalculate_actual_plan_costs` 仍按真实 `truck_routes` 全量距离与 UAV 航段重算 `plan.cost_total` 与 `summary["cost_breakdown"]`，公式与贪心基线一致；**不**把竞价用的 `cost_wait` / `cost_risk` 计入 `cost_dist` / `cost_energy` / `cost_penalty`。

**每轮收尾（全量与增量共用）**：在 `_revoke_stale_drone_assignments` 之后依次调用：

- `_validate_drone_route_closure`（路径闭环自检）
- `_validate_truck_premature_return`（卡车提前回仓告警）
- `_ensure_idle_drones_return_to_depot`（空闲机自动归巢，见 §7）

---

## 2. 时空契约 `RendezvousContract`

**触发**：某订单授标为 **B_WAIT** 且 `feasible` 时，`_allocate_order` 立即调用 `_create_contract`，向 `_active_contracts` 追加一条契约。**B_DYNAMIC** 模式中标后**不**生成契约（无人机独立返航，无需卡车配合回收）。

**主要字段**：

- `contract_id`：`RC-0001` 形式递增。
- `truck_id` / `drone_id` / `order_id` / `anchor_id`（回收站）/ `launch_anchor_id`。
- `arrival_time`：卡车预计到达回收锚点时刻（尽量从 `_build_anchor_timetable` 中该锚点读出）。
- `latest_departure`：卡车在锚点的**可汇合截止时刻**（生死线，用于契约校验与时刻表合并）。
- `uav_arrival_time`：无人机预计抵达回收锚点时刻。
- `t_sync`：`max(uav_arrival, truck_arrival) + TRUCK_DRONE_RECOVER_TIME`，在此之前 `_is_drone_locked` 为真。
- `status`：`active` → `expired` / `fulfilled` / `released`。

**生命周期**：

- `_expire_contracts(current_time)`：在每次 `dispatch` / `dispatch_incremental` 开头执行，`current_time > latest_departure` 的活跃契约标为 `expired`。
- `_try_flexible_recovery(current_time)`：紧随 `_expire_contracts` 之后执行。若无人机等待卡车的剩余时间 > 飞回仓库时间，且电量安全可达仓库，则将契约标为 `released`，释放无人机锁定。
- `fulfill_contract(contract_id)`：供仿真/决策引擎在回收完成后调用，将契约标为 `fulfilled`。`DispatchDecisionEngine.try_fulfill_contracts` 提供自动化调用。

---

## 3. 全量流程 `dispatch`

1. 重置 `_auction_bid_count`、`_auction_award_count`、`_anchor_preview_routes`。
2. `_expire_contracts(current_time)`。
3. `_try_flexible_recovery(current_time)`。
4. `_prepare_anchor_preview_routes`（见 §5）。
5. `super().dispatch(...)`：OSM 加载、按 deadline 排序、逐单 `_allocate_order`、Phase 3 `_build_truck_route`。
6. `_recalculate_actual_plan_costs`。
7. `summary["solver"] = "market"`，`summary["auction_stats"]` 含 `bids`、`awards`、`active_contracts`。
8. `_revoke_stale_drone_assignments`（见 §4，含不可撤销与飞行中保护）。
9. **闭环与归巢**：`_validate_drone_route_closure`、`_validate_truck_premature_return`、`_ensure_idle_drones_return_to_depot`（见 §7）。

---

## 4. 任务锁定与不可撤销（Immutability）

**履约临界点**：订单状态为 **PICKED_UP**、**DELIVERING** 或 **COMPLETED** 时，视为**不可变更资产**；增量调度不得对其重新拍卖，也不得在改派逻辑中撤销其与当前执行者的绑定。

**实现要点**：

- `_is_order_immutable(order_id)`：通过 `MarketBasedSolver._order_mgr`（由 `DispatchDecisionEngine` 在构造/切换求解器时调用 `bind_order_manager` 注入）读取 `assigned_orders`，判断是否已过临界点。**不再**使用 `EntityManager.order_mgr`。
- `_get_order_sole_executor(order_id)`：返回当前 `carrying_order_id == order_id` 的无人机 ID，用于**唯一履约权**诊断。
- `dispatch_incremental`：在调用 `super().dispatch` 前构造 `filtered_orders`，剔除不可变更订单，并打日志 / 统计 `immutable_orders_skipped`。
- `_revoke_stale_drone_assignments`：
  - 若订单已不可变更且「计划中标者」≠ `carrying` 持有者，将对应 `AllocationResult` 标为 `feasible=False` 并写 `reason`，**不**清理原执行机状态。
  - 若其他机上仍有同单残留但该机**正飞行且 carrying 该单**，同样拒绝改派并置中标为不可行。

**与编排层的关系**：订单归档仍以实体层 `carrying_order_id` 与 `assigned_orders` 为准；本层通过锁定绑定，减少「A 机送货、B 机仍挂同单」导致的归档不一致。

---

## 5. 竞标准入（Bidding Eligibility）

无人机投标不再仅依赖基线 `_get_available_drones()`（纯 IDLE、无路径）。由 `_classify_drone_eligibility` 划分准入等级，`_collect_drone_bids` 分三路收集投标。

### 5.1 无人机准入等级

| 等级 | 典型 `DroneStatus` | 准入 | 竞标起点 / 说明 |
|------|-------------------|------|------------------|
| **IDLE** | `IDLE`，无 `carrying`、无等待回收、无待飞路径 | 是 | 当前精确位置 `current_loc`；走 B_WAIT / B_DYNAMIC / C / 可选 B。 |
| **RETURNING** | `RETURNING_TO_DEPOT`、`FLYING_TO_STATION`、`FLYING_TO_TRUCK` | 是（串联/接力） | 当前航路**末点**；需 `E_remain > E_next_task + E_safety`（用 `_estimate_relay_remaining_energy` 与 `ENERGY_SAFETY_FACTOR` 近似）。由 `_build_returning_relay_bids` 生成 Mode **C** 串联标（`_is_relay=True`）。 |
| **DELIVERING** | `FLYING_TO_DELIVER` | 条件准入（预调度） | 仅当距完成当前路径的剩余飞行时间 `T_remain < AUCTION_BUFFER_TIME`；起点为当前任务 **DELIVER** 航点。由 `_build_delivering_preschedule_bids` 生成串联标。 |
| **LOCKED** | 契约锁定 `_is_drone_locked`、等待回收站、关键地面段等 | 否 | — |
| **REJECT** | 已在本轮 `allocated_drones`、载重不足等 | 否 | — |

`FLYING_TO_PICKUP` / `LOADING` / `UNLOADING` 等视为 **LOCKED**，禁止参与竞标，避免打断起飞与装载关键段。

### 5.2 卡车准入与契约冲突

- **Mode A**：在原有 `_validate_truck_insertion` 基础上，增加 `_validate_truck_fleet_safety`：粗估插单绕路对**本车运输中无人机**计划起飞/截止时刻的影响，避免「卡车接单导致已挂载/待起飞 UAV 错过生死线或 deadline」。
- **B_WAIT / B_DYNAMIC / 锚点遍历**：对每辆卡车仍先调用 `_validate_truck_insertion`，保证不破坏已锁定契约时间窗。

---

## 6. 路径拼接与前瞻起点（Path Stitching）

**常量**（类属性，可与仿真节拍对齐调参）：

| 常量 | 默认值 | 含义 |
|------|--------|------|
| `AUCTION_BUFFER_TIME` | 15.0 s | DELIVERING 预调度窗口 \(T_{remain} < T_{auction\_buffer}\)。 |
| `PATH_STITCH_TOLERANCE_M` | 1.0 m | 新路径段首点与旧路径末点（或预测位置）允许的最大间隙。 |
| `AUCTION_COMPUTE_DELTA_T` | 0.5 s | 拍卖计算耗时 \(\Delta t\)，用于移动中实体的**前瞻起点**近似。 |

**实现要点**：

- `_predict_drone_position(drone, current_time, delta_t)`：沿当前未执行航段按匀速线性推进，得到 \(P(t+\Delta t)\)，避免把仓库/订单初始点当作移动中的竞标起点。
- `_apply_path_stitching_constraints`：在 `_allocate_order` 授标后、建契约前对无人机标执行检查；串联标若 `relay_origin` 与当前路径终点间隙大于容差，设置 `_needs_interpolation`、`_interpolation_from` / `_interpolation_to`，供编排层做线性插值补全（实际 `append_route` 仍在 `decision_engine._setup_drone_routes`）。
- `validate_path_continuity(drone_id, new_waypoints, current_time)`：对外 API，供编排层在挂载航路前做一致性校验。

---

## 7. 任务闭环与卡车清空（Global Completeness）

### 7.1 无人机路径闭环自检

`_validate_drone_route_closure(plan)`：对计划中已分配且有机体的无人机，若其**当前** `route_plan` 非空且最后一个航路点动作**不是** `DOCK_DEPOT` / `DOCK_TRUCK`，打告警日志。注意：计划刚生成时部分机体路由可能尚未由决策引擎写入，该检查更偏向**运行中已有路径**的兜底。

### 7.2 空闲机自动归巢

`_ensure_idle_drones_return_to_depot(current_time)`：对 **IDLE**、无订单、无路径、无车载、无等待回收、无卸货挂起的无人机，若距仓库超过约 5 m 且电量足够返仓，则 `set_route` 单点 `DOCK_DEPOT` 并将状态置为 `RETURNING_TO_DEPOT`，减少「无后续动作」悬挂。

### 7.3 卡车回仓条件（清空校验）

`should_truck_return_to_depot(truck_id)` 在以下**全部**满足时返回 `True`（语义上对应「系统级清空」的保守实现）：

- `order_mgr.pending_orders` 为空；
- 该车无 **active** 契约；
- 无无人机以 `transport_truck_id` 绑定该车；
- **全局**无飞行中无人机、无 `carrying_order_id`；
- `order_mgr.assigned_orders` 为空。

`_validate_truck_premature_return(plan)`：若计划末节点为回仓 depot 且上述清空条件不满足，打警告日志（不自动改计划，避免与基线路由构建强耦合）。

---

## 8. 单订单拍卖 `_allocate_order`

1. **超重**：强制卡车，仅 `_best_truck_bid`（走 `_validate_truck_fleet_safety` → 内含 `_validate_truck_insertion`）。
2. **否则**：`_best_truck_bid`（Mode A）+ `_collect_drone_bids`（B_WAIT / B_DYNAMIC / C / 可选 B，以及 **RETURNING / DELIVERING** 串联 C 标）。
3. **Mode A**：卡车插单可行性 + 车队安全（见 §5.2）。
4. **B_WAIT / B_DYNAMIC**：锚点遍历前 `_validate_truck_insertion`。
5. **无人机**：按 §5.1 分级；仍排除 `allocated_drones`；契约锁定 `_is_drone_locked` 在分级中体现为 LOCKED。
6. 授标 `min(score_total)`；对无人机中标结果调用 `_apply_path_stitching_constraints`（§6）；若中标 **B_WAIT**，再 `_create_contract`。

---

## 9. 锚点预览与时刻表

### 9.1 `_prepare_anchor_preview_routes`

- 若任意卡车已有非空 `_planned_route_stops`，**跳过**预览构建，时刻表走运行时计划分支。
- 否则加载 OSM，订单按距各卡车最近归桶，对每辆有单的卡车调用 `_build_truck_route`，其中 **`recovery_station_ids`** 注入该车 **活跃契约**的 `anchor_id`（必经回收站），再写入 `_anchor_preview_routes`。

### 9.2 `_build_anchor_timetable` → `_merge_contracts_into_timetable`

**锚点来源优先级**：预览路线 `station` 节点 → `_planned_route_stops` 的 `station` → `_predict_truck_charging_stations` 启发式。

**契约合并**：

- 已有锚点与契约 `anchor_id` 相同：标记 `locked: True`，`latest_rendezvous_time = min(原值, contract.latest_departure)`。
- 契约锚点不在列表中：插入新锚点，`locked: True`，时间与契约一致。

**缓冲（Slack）**：对 **`locked` 为假** 的锚点，`latest_rendezvous_time = arrival_time + T_MAX_WAIT × (1 - TIMETABLE_SLACK_RATIO)`（默认 Slack 10%）。锁定锚点不再被 Slack 收紧 `latest_departure` 以外的逻辑重复压缩（锁定行使用契约给出的 `latest_departure`）。

最后按 `arrival_time` 排序并重编 `index`。

**硬可行性**：B_WAIT / B 构建投标时，`uav_arrival_recovery > latest_rendezvous_time` 则丢弃该候选。B_DYNAMIC 不受锚点生死线约束（回收目标为仓库）。

---

## 10. 投标模式

### 10.0 投标模式一览

| 模式 | 起飞点 | 回收点 | 生成契约 | 说明 |
|------|--------|--------|----------|------|
| **A** | — | — | 否 | 卡车直递 |
| **B** | 卡车当前位置 | 充电站 | 否 | 移动起飞（可选开关） |
| **B_WAIT** | 锚点充电站 | 锚点充电站 | **是** | 站点起飞 + 站点回收 |
| **B_DYNAMIC** | 锚点充电站 | **仓库** | 否 | 站点起飞 + 仓库回收 |
| **C** | 仓库或串联起点 | 仓库 | 否 | 仓到仓；串联任务中标带 `_is_relay`、`_relay_origin` |

### 10.1 Mode A / C

使用基线 `_score_allocation`（`f_dist + f_energy + f_penalty`）。

### 10.2 Mode B / B_WAIT（`_score_market_b_bid`）

- **基础项（写入 `cost_dist` / `cost_energy` / `cost_penalty`）**：与原先一致 — UAV 全量距离/能耗；卡车仅 **边际** 距离（锚点均在当前时刻表内则为 0）；超时惩罚同基线 `LAMBDA_TIME × penalty_rate × lateness`。
- **竞价附加项（仅进入 `score_total`，不写入上述三字段）**：
  - **非对称等待**：`truck_arrival_at_recovery` 与 `uav_arrival_at_recovery` 比较；若卡车晚到（机等车）`cost_wait = (OMEGA_UAV_IDLE + OPPORTUNITY_COST_PER_SEC) × (T_truck - T_uav)`（含机会成本）；若卡车早到（车等机）`cost_wait = OMEGA_TRUCK_IDLE × |T_uav - T_truck|`。
  - **生死线风险**：`margin = latest_rendezvous - uav_arrival`，`risk_ratio = 1 - min(margin / T_MAX_WAIT, 1)`，`cost_risk = DEADLINE_RISK_WEIGHT × risk_ratio × (cost_dist + cost_energy)`。

`score_total = cost_dist + cost_energy + cost_penalty + cost_wait + cost_risk`。

### 10.3 Mode B_DYNAMIC（`_score_b_dynamic_bid`）

- **基础项**：UAV 全量距离/能耗（launch→customer→depot）；卡车仅边际距离（launch 锚点在时刻表内则为 0）。
- **无 `cost_wait`**（无人机直飞仓库，不等卡车）。
- **无 `cost_risk`**（不依赖锚点生死线）。

`score_total = cost_dist + cost_energy + cost_penalty`。

### 10.4 计划级汇总

`_recalculate_actual_plan_costs` 仅使用真实路网卡车里程、UAV 航段与各 `alloc.cost_penalty` 之和，**不包含** `cost_wait` / `cost_risk`。

---

## 11. 类级常量（代码默认值）

| 常量 | 默认值 | 含义 |
|------|--------|------|
| `T_MAX_WAIT` | 60.0 s | 锚点最大等待窗口（与节点时间推导配合使用） |
| `OMEGA_UAV_IDLE` | 0.5 | 机等车等待惩罚权重 |
| `OMEGA_TRUCK_IDLE` | 1.0 | 车等机等待惩罚权重 |
| `OPPORTUNITY_COST_PER_SEC` | 0.1 | 机等车的额外机会成本（每秒） |
| `TIMETABLE_SLACK_RATIO` | 0.10 | 非锁定锚点汇合窗口收紧比例 |
| `DEADLINE_RISK_WEIGHT` | 0.8 | 贴近生死线时的风险溢价系数 |
| `AUCTION_BUFFER_TIME` | 15.0 s | DELIVERING 预调度缓冲 |
| `PATH_STITCH_TOLERANCE_M` | 1.0 m | 路径拼接坐标容差 |
| `AUCTION_COMPUTE_DELTA_T` | 0.5 s | 前瞻起点时间偏移 \(\Delta t\) |

`TRUCK_DRONE_LAUNCH_TIME`、`TRUCK_DRONE_RECOVER_TIME`、`delivery_service_time`、`ENERGY_SAFETY_FACTOR` 等仍由基线/配置 `drone_params.yaml` 的 `solver_energy` 覆盖。

---

## 12. 诊断日志

- `[MarketBasedSolver][Diag]`：含 `contracts` 活跃数、`excluded_locked`、以及 `returning_drones` / `delivering_drones` 等分级统计（随实现字段可能扩展）。
- `[MarketBasedSolver][Bid]` / `[Selected]`：投标与中标摘要（含 B_DYNAMIC、串联 C）。
- `[MarketBasedSolver][Contract]`：新建、过期、兑现、弹性归巢释放。
- `[MarketBasedSolver] 按真实执行路径重算总成本`：计划级汇总。
- `[MarketBasedSolver][PathStitch]`、`[Closure]`：路径拼接与闭环相关 INFO/WARN。

---

## 13. 求解器注册

工厂名 **`market`**（`backend/solver/factory.py`），与 **`greedy`** 并列。

---

## 14. 已覆盖的优化点（当前版本）

- **动态约束协议**：状态分级准入、履约锁定、路径前瞻与拼接标记、闭环与卡车清空校验、空闲机自动归巢。
- **弹性契约与提前兑现**：`_try_flexible_recovery` 在调度入口自动检查，若无人机等卡车成本高于自主归巢，释放契约并解锁无人机。
- **B_DYNAMIC 混合投标**：站点起飞 + 仓库回收，解决卡车远离仓库但订单靠近仓库时的协同效率问题。
- **评分权重校正**：缩小机等车/车等机的极端不对称性，引入机会成本，降低风险溢价以减少保守退化。
- **全模式插单校验**：B_WAIT 和 B_DYNAMIC 投标前均调用 `_validate_truck_insertion`；Mode A 额外 `_validate_truck_fleet_safety`。
- **增量调度仿真层接入**：`DispatchDecisionEngine.execute_incremental` 和 `try_fulfill_contracts` 已可用。

---

## 15. 尚未覆盖或简化实现的设计点

- **充电/归巢拍卖**（独立归巢轮次）：仍以配送任务拍卖为主；空闲机归巢由 `_ensure_idle_drones_return_to_depot` 启发式补一条返仓航路，而非完整「竞价归巢」市场。
- **`dispatch_incremental` 与预览**：若已有 `_planned_route_stops` 则本轮可能不重建 `_anchor_preview_routes`，此时依赖 `_build_anchor_timetable` 的运行时计划 + 契约合并。
- **`should_truck_return_to_depot`**：为保守全局条件，可能与「单车清空即可回仓」的产品定义不完全一致；若需按车过滤 assigned 订单，可在后续版本收紧/放宽条件并与路线构建联动。
- **插值补全**：`_needs_interpolation` 仅标记意图，实际几何插值需在 `decision_engine` 或实体层消费该标记后实现。

---

## 16. 修改摘要（相对早期仅契约市场版）

| 主题 | 行为变化 |
|------|----------|
| 无人机投标 | 从「仅 IDLE」扩展为 IDLE + RETURNING 串联 + DELIVERING 预调度；PICKUP/装卸等锁定不参与。 |
| 增量调度 | 过滤 `PICKED_UP` / `DELIVERING` / `COMPLETED` 订单；统计 `immutable_orders_skipped`。 |
| 改派清理 | `_revoke_stale_drone_assignments` 尊重不可撤销与飞行中 carrying，可否定中标 `feasible`。 |
| 卡车 Mode A | 增加车队安全校验，减轻插单对已车载 UAV 的冲击。 |
| 路径 | 前瞻 \(\Delta t\)、串联间隙标记、对外 `validate_path_continuity`。 |
| 收尾 | 路径闭环日志、卡车提前回仓告警、空闲机自动归巢。 |

文档版本与 `market_based_solver.py` 内实现同步维护；若接口或常量变更，请同时更新本文 §6、§7、§11。
