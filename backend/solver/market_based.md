# 市场拍卖调度 — 代码实现说明

本文档描述 `backend/solver/market_based_solver.py` 的**当前实现**，与仓库内其他设计/公式文档对照阅读。实现类 `MarketBasedSolver` **继承** `GreedyBaseline`，复用 OSM 路网加载、`_build_truck_route`、`_score_allocation`、`DispatchPlan` / `AllocationResult` 及 `decision_engine` 执行链。

---

## 1. 适用范围与调度入口

| 入口 | 作用 |
|------|------|
| `dispatch(pending_orders, current_time, bbox, scene_id)` | 全量批次：处理本轮传入的全部待分配订单。 |
| `dispatch_incremental(new_orders, current_time, bbox, scene_id)` | 增量：仅对 `new_orders` 跑一轮贪心 Phase 1–3；**尊重**求解器内已存在的 `RendezvousContract`（见下）。`summary["dispatch_type"] = "incremental"`。 |

**说明**：`dispatch_incremental` 不会自动「冻结仿真」；调用方需在适当时机暂停业务逻辑后再调用。契约列表 `_active_contracts` 挂在 **solver 实例**上，跨多次 `dispatch` / `dispatch_incremental` 保留，直至过期或由 `fulfill_contract` 兑现。

**总体能耗/成本汇总不变**：`_recalculate_actual_plan_costs` 仍按真实 `truck_routes` 全量距离与 UAV 航段重算 `plan.cost_total` 与 `summary["cost_breakdown"]`，公式与贪心基线一致；**不**把竞价用的 `cost_wait` / `cost_risk` 计入 `cost_dist` / `cost_energy` / `cost_penalty`。

---

## 2. 时空契约 `RendezvousContract`

**触发**：某订单授标为 **B_WAIT** 且 `feasible` 时，`_allocate_order` 立即调用 `_create_contract`，向 `_active_contracts` 追加一条契约。

**主要字段**：

- `contract_id`：`RC-0001` 形式递增。
- `truck_id` / `drone_id` / `order_id` / `anchor_id`（回收站）/ `launch_anchor_id`。
- `arrival_time`：卡车预计到达回收锚点时刻（尽量从 `_build_anchor_timetable` 中该锚点读出）。
- `latest_departure`：卡车在锚点的**可汇合截止时刻**（生死线，用于契约校验与时刻表合并）。
- `uav_arrival_time`：无人机预计抵达回收锚点时刻。
- `t_sync`：`max(uav_arrival, truck_arrival) + TRUCK_DRONE_RECOVER_TIME`，在此之前 `_is_drone_locked` 为真。
- `status`：`active` → `expired`（超时）或 `fulfilled`（外部调用 `fulfill_contract`）。

**生命周期**：

- `_expire_contracts(current_time)`：在每次 `dispatch` / `dispatch_incremental` 开头执行，`current_time > latest_departure` 的活跃契约标为 `expired`。
- `fulfill_contract(contract_id)`：供仿真/决策引擎在回收完成后调用，将契约标为 `fulfilled`。

---

## 3. 全量流程 `dispatch`

1. 重置 `_auction_bid_count`、`_auction_award_count`、`_anchor_preview_routes`。
2. `_expire_contracts(current_time)`。
3. `_prepare_anchor_preview_routes`（见 §5）。
4. `super().dispatch(...)`：OSM 加载、按 deadline 排序、逐单 `_allocate_order`、Phase 3 `_build_truck_route`。
5. `_recalculate_actual_plan_costs`。
6. `summary["solver"] = "market"`，`summary["auction_stats"]` 含 `bids`、`awards`、`active_contracts`。

---

## 4. 单订单拍卖 `_allocate_order`

1. **超重**：强制卡车，仅 `_best_truck_bid`（同样走契约校验）。
2. **否则**：`_best_truck_bid`（Mode A）+ `_collect_drone_bids`（B_WAIT / C / 可选 B）。
3. **Mode A 契约校验**：`_validate_truck_insertion` — 用直线距离 + 速度粗估「当前位置 → 新单客户 → 各契约锚点」的 ETA，若任一大于 `contract.latest_departure`，该卡车**不出** Mode A 标。（近似 VRPTW 可行性，非完整路网重规划。）
4. **无人机**：排除 `allocated_drones`；另排除 `_is_drone_locked`（`t_sync` 未到）。
5. 授标 `min(score_total)`；若中标 **B_WAIT**，再 `_create_contract`。

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

**硬可行性**：B_WAIT / B 构建投标时，`uav_arrival_recovery > latest_rendezvous_time` 则丢弃该候选。

---

## 6. 评分与成本口径

### 6.1 Mode A / C

使用基线 `_score_allocation`（`f_dist + f_energy + f_penalty`）。

### 6.2 Mode B / B_WAIT（`_score_market_b_bid`）

- **基础项（写入 `cost_dist` / `cost_energy` / `cost_penalty`）**：与原先一致 — UAV 全量距离/能耗；卡车仅 **边际** 距离（锚点均在当前时刻表内则为 0）；超时惩罚同基线 `LAMBDA_TIME × penalty_rate × lateness`。
- **竞价附加项（仅进入 `score_total`，不写入上述三字段）**：
  - **非对称等待**：`truck_arrival_at_recovery` 与 `uav_arrival_at_recovery` 比较；若卡车晚到（机等车）`cost_wait = OMEGA_UAV_IDLE × (T_truck - T_uav)`；若卡车早到（车等机）`cost_wait = OMEGA_TRUCK_IDLE × |T_uav - T_truck|`。
  - **生死线风险**：`margin = latest_rendezvous - uav_arrival`，`risk_ratio = 1 - min(margin / T_MAX_WAIT, 1)`，`cost_risk = DEADLINE_RISK_WEIGHT × risk_ratio × (cost_dist + cost_energy)`。

`score_total = cost_dist + cost_energy + cost_penalty + cost_wait + cost_risk`。

### 6.3 计划级汇总

`_recalculate_actual_plan_costs` 仅使用真实路网卡车里程、UAV 航段与各 `alloc.cost_penalty` 之和，**不包含** `cost_wait` / `cost_risk`。

---

## 7. 类级常量（代码默认值）

| 常量 | 默认值 | 含义 |
|------|--------|------|
| `T_MAX_WAIT` | 60.0 s | 锚点最大等待窗口（与节点时间推导配合使用） |
| `OMEGA_UAV_IDLE` | 0.05 | 机等车等待惩罚权重 |
| `OMEGA_TRUCK_IDLE` | 2.0 | 车等机等待惩罚权重 |
| `TIMETABLE_SLACK_RATIO` | 0.10 | 非锁定锚点汇合窗口收紧比例 |
| `DEADLINE_RISK_WEIGHT` | 1.5 | 贴近生死线时的风险溢价系数 |

`TRUCK_DRONE_LAUNCH_TIME`、`TRUCK_DRONE_RECOVER_TIME`、`delivery_service_time`、`ENERGY_SAFETY_FACTOR` 等仍由基线/配置 `drone_params.yaml` 的 `solver_energy` 覆盖。

---

## 8. 诊断日志

- `[MarketBasedSolver][Diag]`：含 `contracts` 活跃数、`excluded_locked`（契约锁定排除的无人机数）。
- `[MarketBasedSolver][Bid]` / `[Selected]`：投标与中标摘要。
- `[MarketBasedSolver][Contract]`：新建、过期、兑现。
- `[MarketBasedSolver] 按真实执行路径重算总成本`：计划级汇总。

---

## 9. 求解器注册

工厂名 **`market`**（`backend/solver/factory.py`），与 **`greedy`** 并列。

---

## 10. 尚未覆盖的设计点（与理想协议差距）

- **充电/归巢拍卖**（文档 Algorithm Step 4）：代码侧仍以配送任务拍卖为主，未单独实现「低电量 UAV 作为拍卖者」的归巢轮次。
- **Mode B / B_WAIT 的卡车间契约插单**：仅 Mode A 在投标前调用 `_validate_truck_insertion`；无人机协同标未与「会破坏已锁定锚点」做强约束联动（依赖时刻表与后续 Phase 3 路线，而非同一套插单检验）。
- **`dispatch_incremental` 与预览**：若已有 `_planned_route_stops` 则本轮可能不重建 `_anchor_preview_routes`，此时依赖 `_build_anchor_timetable` 的运行时计划 + 契约合并。
