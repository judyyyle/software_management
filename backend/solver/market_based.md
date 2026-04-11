# 市场拍卖调度 — 代码实现说明

本文档描述 `backend/solver/market_based_solver.py` 的**当前实现**，与仓库内其他设计/公式文档对照阅读。实现类 `MarketBasedSolver` **继承** `GreedyBaseline`，复用 OSM 路网加载、`_build_truck_route`、`_score_allocation`、`DispatchPlan` / `AllocationResult` 及 `decision_engine` 执行链。

---

## 1. 适用范围与调度入口

| 入口 | 作用 |
|------|------|
| `dispatch(pending_orders, current_time, bbox, scene_id)` | 全量批次：处理本轮传入的全部待分配订单。 |
| `dispatch_incremental(new_orders, current_time, bbox, scene_id)` | 增量：仅对 `new_orders` 跑一轮贪心 Phase 1–3；**尊重**求解器内已存在的 `RendezvousContract`（见下）。`summary["dispatch_type"] = "incremental"`。 |

**说明**：`dispatch_incremental` 不会自动「冻结仿真」；调用方需在适当时机暂停业务逻辑后再调用。契约列表 `_active_contracts` 挂在 **solver 实例**上，跨多次 `dispatch` / `dispatch_incremental` 保留，直至过期、由 `fulfill_contract` 兑现或被弹性归巢释放。

**仿真层接入**：`DispatchDecisionEngine` 提供 `execute_incremental(new_orders, current_time, bbox, scene_id)` 方法，自动调用 `dispatch_incremental` 并应用分配方案。`try_fulfill_contracts(current_time)` 可在每个仿真 tick 中调用，自动兑现已完成回收的契约。

**总体能耗/成本汇总不变**：`_recalculate_actual_plan_costs` 仍按真实 `truck_routes` 全量距离与 UAV 航段重算 `plan.cost_total` 与 `summary["cost_breakdown"]`，公式与贪心基线一致；**不**把竞价用的 `cost_wait` / `cost_risk` 计入 `cost_dist` / `cost_energy` / `cost_penalty`。

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

---

## 4. 单订单拍卖 `_allocate_order`

1. **超重**：强制卡车，仅 `_best_truck_bid`（同样走契约校验）。
2. **否则**：`_best_truck_bid`（Mode A）+ `_collect_drone_bids`（B_WAIT / B_DYNAMIC / C / 可选 B）。
3. **Mode A 契约校验**：`_validate_truck_insertion` — 用直线距离 + 速度粗估「当前位置 → 新单客户 → 各契约锚点」的 ETA，若任一大于 `contract.latest_departure`，该卡车**不出** Mode A 标。（近似 VRPTW 可行性，非完整路网重规划。）
4. **B_WAIT / B_DYNAMIC 契约校验**：`_collect_anchor_bids_for_drone` 和 `_collect_b_dynamic_bids` 遍历卡车前均调用 `_validate_truck_insertion`，确保插单不会导致已锁定契约超时。
5. **无人机**：排除 `allocated_drones`；另排除 `_is_drone_locked`（`t_sync` 未到且 status 仍为 `active`）。
6. 授标 `min(score_total)`；若中标 **B_WAIT**，再 `_create_contract`。

---

## 5. 锚点预览与时刻表

### 5.1 `_prepare_anchor_preview_routes`

- 若任意卡车已有非空 `_planned_route_stops`，**跳过**预览构建，时刻表走运行时计划分支。
- 否则加载 OSM，订单按距各卡车最近归桶，对每辆有单的卡车调用 `_build_truck_route`，其中 **`recovery_station_ids`** 注入该车 **活跃契约**的 `anchor_id`（必经回收站），再写入 `_anchor_preview_routes`。

### 5.2 `_build_anchor_timetable` → `_merge_contracts_into_timetable`

**锚点来源优先级**：预览路线 `station` 节点 → `_planned_route_stops` 的 `station` → `_predict_truck_charging_stations` 启发式。

**契约合并**：

- 已有锚点与契约 `anchor_id` 相同：标记 `locked: True`，`latest_rendezvous_time = min(原值, contract.latest_departure)`。
- 契约锚点不在列表中：插入新锚点，`locked: True`，时间与契约一致。

**缓冲（Slack）**：对 **`locked` 为假** 的锚点，`latest_rendezvous_time = arrival_time + T_MAX_WAIT × (1 - TIMETABLE_SLACK_RATIO)`（默认 Slack 10%）。锁定锚点不再被 Slack 收紧 `latest_departure` 以外的逻辑重复压缩（锁定行使用契约给出的 `latest_departure`）。

最后按 `arrival_time` 排序并重编 `index`。

**硬可行性**：B_WAIT / B 构建投标时，`uav_arrival_recovery > latest_rendezvous_time` 则丢弃该候选。B_DYNAMIC 不受锚点生死线约束（回收目标为仓库）。

---

## 6. 投标模式

### 6.0 投标模式一览

| 模式 | 起飞点 | 回收点 | 生成契约 | 说明 |
|------|--------|--------|----------|------|
| **A** | — | — | 否 | 卡车直递 |
| **B** | 卡车当前位置 | 充电站 | 否 | 移动起飞（可选开关） |
| **B_WAIT** | 锚点充电站 | 锚点充电站 | **是** | 站点起飞 + 站点回收 |
| **B_DYNAMIC** | 锚点充电站 | **仓库** | 否 | 站点起飞 + 仓库回收（新增） |
| **C** | 仓库 | 仓库 | 否 | 仓库直发 + 仓库回收 |

### 6.1 Mode A / C

使用基线 `_score_allocation`（`f_dist + f_energy + f_penalty`）。

### 6.2 Mode B / B_WAIT（`_score_market_b_bid`）

- **基础项（写入 `cost_dist` / `cost_energy` / `cost_penalty`）**：与原先一致 — UAV 全量距离/能耗；卡车仅 **边际** 距离（锚点均在当前时刻表内则为 0）；超时惩罚同基线 `LAMBDA_TIME × penalty_rate × lateness`。
- **竞价附加项（仅进入 `score_total`，不写入上述三字段）**：
  - **非对称等待**：`truck_arrival_at_recovery` 与 `uav_arrival_at_recovery` 比较；若卡车晚到（机等车）`cost_wait = (OMEGA_UAV_IDLE + OPPORTUNITY_COST_PER_SEC) × (T_truck - T_uav)`（含机会成本）；若卡车早到（车等机）`cost_wait = OMEGA_TRUCK_IDLE × |T_uav - T_truck|`。
  - **生死线风险**：`margin = latest_rendezvous - uav_arrival`，`risk_ratio = 1 - min(margin / T_MAX_WAIT, 1)`，`cost_risk = DEADLINE_RISK_WEIGHT × risk_ratio × (cost_dist + cost_energy)`。

`score_total = cost_dist + cost_energy + cost_penalty + cost_wait + cost_risk`。

### 6.3 Mode B_DYNAMIC（`_score_b_dynamic_bid`）

- **基础项**：UAV 全量距离/能耗（launch→customer→depot）；卡车仅边际距离（launch 锚点在时刻表内则为 0）。
- **无 `cost_wait`**（无人机直飞仓库，不等卡车）。
- **无 `cost_risk`**（不依赖锚点生死线）。

`score_total = cost_dist + cost_energy + cost_penalty`。

### 6.4 计划级汇总

`_recalculate_actual_plan_costs` 仅使用真实路网卡车里程、UAV 航段与各 `alloc.cost_penalty` 之和，**不包含** `cost_wait` / `cost_risk`。

---

## 7. 类级常量（代码默认值）

| 常量 | 默认值 | 含义 |
|------|--------|------|
| `T_MAX_WAIT` | 60.0 s | 锚点最大等待窗口（与节点时间推导配合使用） |
| `OMEGA_UAV_IDLE` | 0.5 | 机等车等待惩罚权重 |
| `OMEGA_TRUCK_IDLE` | 1.0 | 车等机等待惩罚权重 |
| `OPPORTUNITY_COST_PER_SEC` | 0.1 | 机等车的额外机会成本（每秒） |
| `TIMETABLE_SLACK_RATIO` | 0.10 | 非锁定锚点汇合窗口收紧比例 |
| `DEADLINE_RISK_WEIGHT` | 0.8 | 贴近生死线时的风险溢价系数 |

`TRUCK_DRONE_LAUNCH_TIME`、`TRUCK_DRONE_RECOVER_TIME`、`delivery_service_time`、`ENERGY_SAFETY_FACTOR` 等仍由基线/配置 `drone_params.yaml` 的 `solver_energy` 覆盖。

---

## 8. 诊断日志

- `[MarketBasedSolver][Diag]`：含 `contracts` 活跃数、`excluded_locked`（契约锁定排除的无人机数）。
- `[MarketBasedSolver][Bid]` / `[Selected]`：投标与中标摘要（含 B_DYNAMIC 模式）。
- `[MarketBasedSolver][Contract]`：新建、过期、兑现、弹性归巢释放。
- `[MarketBasedSolver] 按真实执行路径重算总成本`：计划级汇总。

---

## 9. 求解器注册

工厂名 **`market`**（`backend/solver/factory.py`），与 **`greedy`** 并列。

---

## 10. 已覆盖的优化点

- **弹性契约与提前兑现**：`_try_flexible_recovery` 在调度入口自动检查，若无人机等卡车成本高于自主归巢，释放契约并解锁无人机。
- **B_DYNAMIC 混合投标**：站点起飞 + 仓库回收，解决卡车远离仓库但订单靠近仓库时的协同效率问题。
- **评分权重校正**：缩小机等车/车等机的极端不对称性（40× → 2×），引入机会成本，降低风险溢价以减少保守退化。
- **全模式插单校验**：B_WAIT 和 B_DYNAMIC 投标前均调用 `_validate_truck_insertion`，确保不会破坏已锁定契约。
- **增量调度仿真层接入**：`DispatchDecisionEngine.execute_incremental` 和 `try_fulfill_contracts` 已可用。

## 11. 尚未覆盖的设计点

- **充电/归巢拍卖**（文档 Algorithm Step 4）：代码侧仍以配送任务拍卖为主，未单独实现「低电量 UAV 作为拍卖者」的归巢轮次。
- **`dispatch_incremental` 与预览**：若已有 `_planned_route_stops` 则本轮可能不重建 `_anchor_preview_routes`，此时依赖 `_build_anchor_timetable` 的运行时计划 + 契约合并。
