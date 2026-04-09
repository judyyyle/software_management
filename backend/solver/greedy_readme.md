# Greedy Solver README

## 1. 这个贪心算法在做什么？

当前版本的贪心求解器是 `GreedyBaseline`（见 `backend/solver/greedy_baseline.py`），核心目标是：

1. 尽快给每个待分配订单找到一个可执行模式（优先空地协同）。
2. 在分配时做前瞻能量校验，避免“派得出去但回不来”的无人机任务。
3. 给卡车构建可执行路径（基于 OSM 路网），并把无人机回收点纳入路径。

一句话概括：
**按订单紧迫度排序 + 按模式优先级逐单尝试 + 先保可行再求近似最优**。

---

## 2. 算法大致流程（当前实现）

在 `GreedyBaseline.dispatch()` 里，流程分三阶段：

### Phase 1: 订单排序

- 输入是 `pending_orders`。
- 先按 `(deadline - current_time, deadline)` 升序排，越紧急越先分。

### Phase 2: 逐单分配

每个订单按以下优先级尝试：

1. `B_WAIT`（无人机在充电站等待后起飞）
2. `B`（卡车当前位置起飞）
3. `C`（仓-空直递）
4. `A`（卡车直递兜底）

关键约束：

- 无人机可用性：状态必须 `IDLE` 且电量高于底线。
- 载重匹配：无人机载重能力 >= 订单载重。
- 前瞻能量校验：
  - 送达到客户 + 飞到回收点的总能耗（含安全系数）必须 <= 当前电量。
- 防重复分配：同一轮调度里同一无人机不会被分给多个订单。

### Phase 3: 卡车路径构建

- 对模式 A 订单 + 模式 B/B_WAIT 的回收站点一起做卡车路径。
- 路径使用 OSM 路网最短路，不走“纯欧氏距离回退”。
- 采用最近邻启发式，并尝试顺路插入充电站（受绕路比例约束）。

输出是 `DispatchPlan`：

- `allocations`：每个订单分配结果
- `truck_routes`：每辆卡车的关键节点与几何路径
- `summary`：本轮可行数、模式分布、总成本等

---

## 3. 与编排层怎么衔接？

`DispatchDecisionEngine`（`backend/solver/decision_engine.py`）负责：

1. 调用 `GreedyBaseline.dispatch()` 生成方案。
2. 把分配写回订单状态机（`pending -> assigned -> ...`）。
3. 把卡车/无人机路径下发到实体对象。
4. 输出日志给前端展示。

可以理解为：

- `greedy_baseline.py` = “算方案”
- `decision_engine.py` = “执行方案”

---

## 4. 后续加新算法，放在哪里？

你说的方向是对的，建议都放在 `backend/solver` 下，按“编排层 + 算法层”分开。

推荐结构：

```text
backend/solver/
  decision_engine.py              # 统一编排入口
  greedy_baseline.py              # 当前贪心基线
  algorithms/                     # 新算法目录（建议新增）
    alns_solver.py
    ga_solver.py
    drl_policy.py
  interfaces.py                   # （建议新增）求解器协议/抽象基类
  factory.py                      # （建议新增）按配置选择求解器
```

说明：

1. `decision_engine.py` 不要写具体算法细节，只做流程编排和状态落地。
2. 每个算法模块尽量暴露统一接口（比如都实现 `dispatch(...) -> DispatchPlan`）。
3. 通过 `factory.py` 或配置项切换算法，避免在编排层大量 `if/else`。

---

## 5. 新算法接入的最小改动建议

如果要加一个 `ALNS`，建议步骤：

1. 在 `backend/solver/algorithms/alns_solver.py` 实现 `dispatch(...)`。
2. 保持返回类型与 `DispatchPlan` 一致（或能转换为 `DispatchPlan`）。
3. 在 `decision_engine.py` 里通过配置选择 `self.solver = ALNSSolver(...)`。
4. 复用现有 `_apply_plan()`、`_setup_drone_routes()` 等执行逻辑。

这样可以做到：

- 算法层可替换
- 执行层稳定
- 前端接口基本不变

---

## 6. 当前版本定位

`GreedyBaseline` 适合做：

- 快速可运行 baseline
- 联调实体状态机与前端可视化
- 为 ALNS/GA/DRL 提供对比下界

但它本质是启发式近似，不保证全局最优。后续如果追求更优解和更稳定的时空协同，建议在 `backend/solver/algorithms` 下逐步引入 ALNS 或学习型策略。