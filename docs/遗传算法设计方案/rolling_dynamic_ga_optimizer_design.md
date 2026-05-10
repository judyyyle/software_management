# GA-MMCE 持续滚动动态重规划优化器设计方案

## 0. 设计目标

当前动态重规划不应被看成一次次彼此独立的 GA 求解，而应改造成一个**持续滚动的 GA 优化器**：

```text
动态订单到来
  -> 立即复用历史 incumbent / archive 产出可执行方案
  -> 在当前时间预算内继续从历史种群演化
  -> 把本轮优秀个体、候选评估、距离计算沉淀到缓存
  -> 仿真空闲 tick 继续小步优化 archive
  -> 下一次动态触发从已有搜索成果继续，而不是从零初始化
```

工程目标不是严格保证每次触发都得到数学意义上的全局最优，而是：

1. 每次触发先快速得到一个可执行且不差的 incumbent plan。
2. 在有限预算内利用历史种群和缓存减少重复计算。
3. 即使 `actual_generations = 0`，初始可选解里也保留 B/C 空地协同结构。
4. 后续空闲时间继续演化，让下一次重规划复用更好的搜索空间。

---

## 1. 当前动态重规划机制

### 1.1 外层触发链路

动态订单和重规划从前端/接口进入仿真后，当前主要链路是：

```text
frontend scheduled_dynamic_orders
  -> backend/api/routes/simulation_bp.py
  -> OrderManager 注入动态订单
  -> backend/environment/state/sim_engine.py::_try_auto_dispatch()
  -> backend/solver/decision_engine.py::execute_incremental()
  -> solver.should_replan_unfinished()
  -> solver.dispatch_replan_current_state()
  -> backend/solver/ga_mmce/solver.py::dispatch_replan_current_state()
  -> backend/solver/ga_mmce/dynamic_rescheduler.py::reschedule_on_event()
```

相关文件：

```text
backend/api/routes/simulation_bp.py
backend/environment/state/sim_engine.py
backend/solver/decision_engine.py
backend/solver/interfaces.py
backend/solver/ga_mmce/solver.py
backend/solver/ga_mmce/dynamic_rescheduler.py
```

运行时路线执行和无人机动作回调相关：

```text
backend/solver/ga_mmce/runtime_adapter.py
backend/environment/state/entity_manager.py
```

### 1.2 GA-MMCE 内部动态重规划流程

当前 `dynamic_rescheduler.reschedule_on_event()` 的核心流程是：

```text
1. _advance_or_snapshot_state()
   取得 event_time 下的运行态快照

2. 读取 solver.last_best_individual / last_best_decode_result
   只拿上一轮 best 个体和 best plan

3. _classify_orders()
   将订单分为 completed / locked / pending / new

4. _select_reoptimization_window()
   从 pending + new 中选择 reoptimized_ids
   其余可继续沿用的未来段进入 frozen_future_ids

5. _build_dynamic_snapshot()
   写入 _ga_time / _ga_position / _ga_host_type / _ga_energy 等 GA 虚拟运行态字段

6. build_ga_context()
   根据当前资源快照生成 gene_pool、truck_drone_ids、depot_drone_ids、support_node_ids

7. build_warm_start_population()
   基于 previous_best 生成 warm starts

8. solver.solve(..., dispatch_type="dynamic_replan")
   进入正常 GA 初始化、评估、进化

9. 若 GA 不可行，尝试 fallback
   warm_start_repaired -> greedy_insertion -> previous_plan_unserved_new

10. _annotate_dynamic_summary() / _write_dynamic_replan_csv()
```

核心文件：

```text
backend/solver/ga_mmce/dynamic_rescheduler.py
backend/solver/ga_mmce/adapters.py
backend/solver/ga_mmce/population.py
backend/solver/ga_mmce/solver.py
backend/solver/ga_mmce/decoder.py
backend/solver/ga_mmce/physical_evaluator.py
backend/solver/ga_mmce/config.py
```

### 1.3 当前动态配置

当前 `DYNAMIC_GA_CONFIG` 大致是：

```text
population_size = 30
max_generations = 60
min_generations = 15
early_stopping_patience = 10
time_budget_seconds = 3.0
warm_start_mutations = 10
reopt_window_size = 8
```

这说明设计上希望动态重规划小种群、短预算，但当前实际瓶颈在于：进入 generation loop 前，初始种群评估、B/C precheck、距离和候选评估已经消耗大量预算。

---

## 2. 当前为什么慢，为什么容易退化为全 A

### 2.1 只复用 previous_best，历史搜索空间丢失

当前动态复用主要是：

```python
previous_best = copy.deepcopy(getattr(solver, "last_best_individual", None))
previous_decode = getattr(solver, "last_best_decode_result", None)
```

然后 `build_warm_start_population()` 围绕 `previous_best` 进行修补和 mutation。

这会丢掉上一轮 population 中大量有价值但不是 best 的结构，例如：

```text
低等待 B-heavy 个体
C-heavy 个体
B/C 多但总成本略高的个体
可行但有不同 rendezvous 结构的个体
延迟低但能耗略高的个体
```

动态订单一来，这些结构又要靠随机初始化或变异重新碰出来。

### 2.2 初始评估成本太高，generation loop 可能还没开始就超时

`solver.solve()` 当前顺序是：

```text
build_ga_context()
_prepare_distance_context()
_build_greedy_seed()
_build_b_precheck_and_seed_data()
_prepare_warm_start_seeds()
initialize_population()
_evaluate_population()
for generation in range(...):
    if timeout: break
```

因此预算检查主要发生在初始评估之后和 generation loop 内。动态预算很紧时会出现：

```text
initial population 已评估完成
time_budget_hit = True
actual_generations = 0
best = 初始种群中的某个个体
```

如果初始 best 偏 A，输出就会是全 A 或 A-heavy。

### 2.3 候选和距离重复计算严重

`physical_evaluator.py` 的 A/B/C 候选评估会反复计算：

```text
truck road distance
UAV straight distance
UAV energy
payload feasibility
launch/recover legality
truck wait / UAV wait
deadline lateness
station/recovery soft penalty
```

相邻两次动态重规划中，大量组合只是时间、位置轻微变化，但当前没有候选缓存和距离缓存，导致重复开销很高。

---

## 3. 目标架构

目标是把动态 GA 改成以下结构：

```text
RollingGAOptimizer
  |
  |-- Archive：保留历史优秀种群，而不是只保留 previous_best
  |
  |-- Fast Incumbent：动态触发后先快速评估少量历史种子，得到可执行方案
  |
  |-- Candidate Cache：缓存 A/B/C 候选评估
  |
  |-- Distance Cache：缓存 road distance / UAV distance / UAV energy
  |
  |-- Archive Warm Start Adapter：把历史个体适配到当前订单池和当前 gene_pool
  |
  |-- Anytime Slice：仿真空闲 tick 继续小步演化 archive
  |
  |-- Incremental Decode：中长期保存 prefix trace，只重评估变化后缀
```

推荐落地优先级：

```text
P0：保留并复用动态 archive
P1：增加 fast incumbent，避免 0 代全 A
P2：增加 distance cache
P3：增加 candidate cache
P4：增加 time-sliced anytime GA
P5：增加 incremental decode / prefix trace
```

---

## 4. 第一阶段：动态 Archive

### 4.1 新增文件

建议新增：

```text
backend/solver/ga_mmce/archive.py
```

职责：

```text
1. 保存上一轮和历史轮次的优秀 Individual。
2. 按多种结构保留个体，而不是只按 fitness 排序。
3. 提供可序列化、可修剪、可去重的 archive。
4. 为 dynamic_rescheduler 提供 archive seeds。
```

### 4.2 ArchiveEntry 字段

建议结构：

```text
ArchiveEntry
  individual: Individual
  fitness: float
  cost_total: float
  a_count: int
  b_count: int
  c_count: int
  waiting_cost: float
  delay_cost: float
  repair_penalty: float
  station_queue_penalty: float
  source: str
  generation: int
  event_time: float
  order_signature: tuple
  resource_signature: tuple
```

`order_signature` 和 `resource_signature` 暂时可以用于诊断；第一阶段不必严格作为复用门槛，因为动态适配会再次校验 gene_pool 和 fixed tail。

### 4.3 Archive 修剪策略

不能只保留 top fitness，否则 B/C 结构仍然可能被 A-heavy 个体挤掉。建议分桶保留：

```text
1. fitness 最低 top N
2. B-heavy top N
3. C-heavy top N
4. B+C-heavy top N
5. waiting_cost 低 top N
6. delay_cost 低 top N
7. 最近若干个体
```

最终去重签名：

```text
(
  tuple(individual.sequence),
  tuple(individual.assignment),
  normalized_rendezvous_tuple,
)
```

### 4.4 修改 solver.py

在 `GAMMCESolver.__init__()` 增加：

```text
self.last_population = []
self.dynamic_archive = GAArchive(max_size=config.dynamic_archive_size)
self.dynamic_plan_generation = 0
```

在 `solve()` 中：

```text
1. 初始 population 评估后，把 top-K 加入 archive。
2. 每次 _record_generation() 附近，把当代 top-K 加入 archive。
3. 最终返回前，把最终 top-K 和 best 加入 archive。
4. 保存 self.last_population，供下一次动态重规划复用。
```

建议新增内部方法：

```text
_update_dynamic_archive(population, source, generation, event_time)
```

只在 `dispatch_type == "dynamic_replan"` 或 anytime 优化时更新。

### 4.5 修改 dynamic_rescheduler.py

当前 warm start 只有：

```text
build_warm_start_population(previous_best=...)
```

建议变成：

```text
archive_warm_starts = build_archive_warm_starts(...)
previous_best_warm_starts = build_warm_start_population(...)
warm_starts = merge_warm_starts(
    archive_warm_starts,
    previous_best_warm_starts,
    max_count=dynamic_config.dynamic_warm_start_limit,
)
```

新增函数：

```text
build_archive_warm_starts()
adapt_individual_to_dynamic_orders()
repair_gene_against_current_gene_pool()
repair_rendezvous_against_current_nodes()
merge_warm_starts()
```

### 4.6 Archive 个体适配规则

历史个体不能直接放入当前 population，必须适配：

```text
1. 删除 completed / locked 订单。
2. 删除当前 planning_orders 中不存在的订单。
3. 保留当前仍存在订单的 sequence / assignment / rendezvous。
4. 如果历史 gene 不在当前 gene_pool 中，则降级为 A。
5. 如果 rendezvous 节点不在当前 support_node_ids 中，则重新生成 rendezvous。
6. 对当前新增订单，按策略插入 sequence。
7. 对 frozen_future_ids，强制套用 fixed_tail_gene_by_order。
8. validate_with_context() 失败则丢弃。
```

新增订单插入策略建议按优先级：

```text
1. 插入 previous_best 中相邻地理位置附近。
2. 插入所有位置试少量候选，选 local score 最低。
3. 超预算时直接 append，并用 fast incumbent 修补。
```

第一阶段可先采用简单 append 或 deadline 排序，后续再增强。

---

## 5. 第二阶段：Fast Incumbent

### 5.1 目标

动态触发时不要先赌完整 GA。应先用历史 seeds 快速评估出一个可执行 plan：

```text
archive seeds + previous_best seeds + combined B seed
  -> 评估前 K 个
  -> 得到 best feasible incumbent
  -> 再进入 solver.solve()
```

这样即使 `solver.solve()` 里 `actual_generations = 0`，也可以有一个明确的历史 incumbent 作为兜底。

### 5.2 修改 dynamic_rescheduler.py

在调用 `solver.solve()` 前增加：

```text
fast_plan = _fast_incumbent_plan(
    solver=solver,
    snapshot=snapshot,
    seeds=warm_starts,
    config=dynamic_config,
    planning_orders=planning_orders,
    max_eval=dynamic_config.fast_incumbent_eval_count,
)
```

第一阶段不必改 `solver.solve()` 签名，可以在 `reschedule_on_event()` 中做返回选择：

```text
如果 ga_plan 不可行，优先 fast_plan。
如果 ga_plan 可行但 time_budget_hit 且 actual_generations == 0，
    可以比较 ga_plan.cost_total 和 fast_plan.cost_total，取更优者。
```

更理想的第二步是扩展 `solver.solve(..., incumbent_plan=fast_plan)`，让 solver 内部把 incumbent 作为 best-so-far。

### 5.3 Fast Incumbent 评价范围

不要评估太多，否则会吃掉动态预算。建议：

```text
fast_incumbent_eval_count = 8 ~ 12
fast_incumbent_budget_seconds = 0.2 ~ 0.5
```

输入 seeds 排序优先级：

```text
1. previous_best 修补个体
2. archive fitness elite
3. B-heavy / C-heavy elite
4. combined B seed
5. greedy seed / truck-only seed
```

---

## 6. 第三阶段：Distance Cache

### 6.1 修改文件

```text
backend/solver/ga_mmce/physical_evaluator.py
```

### 6.2 缓存对象挂载位置

不要挂在每次新建的 evaluator 临时局部上。推荐挂到 solver 长生命周期对象之一：

```text
solver.greedy_helper._ga_distance_cache
solver.greedy_helper._ga_candidate_cache
```

当前 `PhysicalEvaluator` 已经持有 `greedy_helper`，因此可以在 evaluator 初始化时确保缓存存在。

### 6.3 缓存内容

优先缓存：

```text
road distance: greedy._road_dist(pos_a, pos_b)
UAV distance: greedy._dist(pos_a, pos_b)
truck energy: greedy._truck_energy_wh(distance)
UAV energy: greedy._uav_energy_wh(drone, pos_a, pos_b, payload)
flight energy: greedy._flight_energy(drone, pos_a, pos_b, payload)
nearest support node lookup
```

### 6.4 key 设计

距离缓存可以用位置 bucket：

```text
("road", pos_bucket_a, pos_bucket_b)
("uav_dist", pos_bucket_a, pos_bucket_b)
("uav_energy", drone_model_sig, pos_bucket_a, pos_bucket_b, payload_bucket)
```

建议 bucket：

```text
road_position_bucket_m = 10.0
uav_position_bucket_m = 5.0
payload_bucket_kg = 0.1
```

最终 best plan 可做一次严格重评估，避免 bucket 近似带来的误差累积。

---

## 7. 第四阶段：Candidate Cache

### 7.1 修改文件

```text
backend/solver/ga_mmce/physical_evaluator.py
```

包装以下函数：

```text
evaluate_fixed_mode_a()
evaluate_fixed_mode_b()
evaluate_fixed_mode_c()
```

将原主体挪为：

```text
_evaluate_fixed_mode_a_uncached()
_evaluate_fixed_mode_b_uncached()
_evaluate_fixed_mode_c_uncached()
```

外层先查 cache，miss 再调用 uncached。

### 7.2 candidate key 必须包含动态状态

不能只用：

```text
(order_id, mode, drone_id, launch, recover)
```

必须包含影响可行性和时间的状态签名：

```text
order_sig:
  payload_weight
  deadline bucket
  delivery_loc bucket

truck_sig:
  truck_id
  _ga_time bucket
  _ga_position bucket
  _ga_node_id

drone_sig:
  drone_id
  _ga_time bucket
  _ga_position bucket
  _ga_energy bucket
  _ga_host_type
  _ga_host_node_id
  _ga_transport_truck_id
  _ga_waiting_station_id

config_sig:
  soft_rendezvous_violation
  truck_wait_max_s
  allow_depot_drone_recover_at_station
  air_ground_mode_reward
  weight_energy / weight_delay / weight_waiting
```

### 7.3 复用策略

Candidate cache 有近似风险，建议分层使用：

```text
1. 初始 precheck、候选排序、archive seed 快速评价：允许使用 bucket cache。
2. 最终 best individual：强制 strict decode 一次，可绕过 candidate cache 或使用更细 bucket。
3. 发现 cache 命中但 plan 出现非法 host/energy 时，自动 invalidate 当前 key。
```

---

## 8. 第五阶段：population.py 动态种群配比

### 8.1 当前逻辑

`initialize_population()` 当前顺序：

```text
warm_start
greedy_seed
truck_only_seed
OBL seed
combined B seed
single B seeds
balanced random
random
```

这个基础很好，但动态场景应让 archive/warm start 占主要比例，而不是让随机个体和 truck-only seed 占太多预算。

### 8.2 新增配置

在 `GAConfig` 和 `DYNAMIC_GA_CONFIG` 中新增：

```text
dynamic_archive_enabled = True
dynamic_archive_size = 60
dynamic_archive_seed_count = 24
dynamic_archive_update_top_k = 20
dynamic_warm_start_limit = 30
dynamic_warm_start_target_ratio = 0.70
dynamic_random_seed_ratio = 0.20
dynamic_force_combined_b_seed = True
```

### 8.3 动态初始化策略

当 `context.mode == "dynamic"` 或 `dispatch_type == "dynamic_replan"` 时：

```text
population_size = 30

目标：
  20 ~ 24 个来自 archive/warm_start
  1 个 previous_best 修补
  1 个 combined B seed
  1 个 greedy seed
  1 个 truck-only seed
  剩余 4 ~ 7 个 balanced/random
```

这样就算没有进化代数，初始 population 中也会含有大量历史 B/C 协同结构。

---

## 9. 第六阶段：Time-Sliced Anytime GA

### 9.1 不建议第一版上后台线程

真实后台线程会和仿真状态、实体 route、runtime adapter 竞争可变对象，容易出现状态污染。第一版推荐单线程 time-sliced：

```text
每个仿真 tick：
  正常推进实体
  检查动态订单
  如果没有新动态订单、没有正在应用 dispatch：
      solver.improve_archive_slice(snapshot, budget=0.03s ~ 0.1s)
```

### 9.2 新增 solver 方法

在 `backend/solver/ga_mmce/solver.py` 新增：

```text
improve_archive_slice(state, config=None, time_budget_seconds=0.05)
```

职责：

```text
1. clone 当前 state。
2. build_ga_context(mode="dynamic")。
3. 从 dynamic_archive 构造 seeds。
4. 小种群初始化。
5. 跑极少量 generation 或到时间片结束。
6. 只更新 dynamic_archive，不应用 runtime plan。
```

### 9.3 外层调用位置

优先考虑：

```text
backend/environment/state/sim_engine.py::_try_auto_dispatch()
```

或封装在：

```text
backend/solver/decision_engine.py
```

调用原则：

```text
1. 没有新订单触发时才运行。
2. 每 tick 时间片必须很小。
3. 不直接改变实体状态，只更新 solver.dynamic_archive。
4. 如果当前 solver 不是 ga_mmce，直接跳过。
```

---

## 10. 第七阶段：Incremental Decode / Prefix Trace

这是中长期优化，收益大但改动也最大。

### 10.1 新增 DecodeTrace

建议在 `decoder.py` 或新文件 `decode_trace.py` 中定义：

```text
DecodeTrace
  sequence
  assignment
  rendezvous
  state_after_index
  allocations_after_index
  partial_cost_after_index
  diagnostics_after_index
```

每个 elite individual 可保存：

```text
individual._ga_decode_trace
```

### 10.2 Dirty index

当 child 来自 parent mutation/crossover 时，计算最早变化位置：

```text
first index where:
  sequence differs
  assignment differs
  rendezvous differs
```

如果 `dirty_idx > 0` 且 prefix trace 仍匹配，则从 `dirty_idx - 1` 的状态继续 decode，只重算后缀。

### 10.3 限制

B/C 会改变无人机 host、energy、waiting station、truck timing，所以 prefix trace 只能在以下条件下复用：

```text
1. prefix sequence 完全一致
2. prefix assignment 完全一致
3. prefix rendezvous 完全一致
4. 当前 dynamic snapshot 的 locked/frozen 资源签名未变化
```

否则必须完整 decode。

---

## 11. Runtime 安全应用规则

如果后续 anytime GA 找到了更优 plan，不建议立刻覆盖 runtime route。建议只在安全切换点应用：

```text
1. 下一次动态订单触发时。
2. 卡车到达 depot/station，处于可重规划节点。
3. 无人机没有 flying / carrying / critical recovery。
4. 新 plan 不改变 completed / locked / already-started 订单。
5. 新 plan 只改 pending 或 assigned-but-not-started 订单。
```

涉及文件：

```text
backend/solver/ga_mmce/runtime_adapter.py
backend/environment/state/entity_manager.py
backend/solver/decision_engine.py
```

建议给 plan summary 增加：

```text
plan_generation
archive_based
fast_incumbent_used
anytime_improved
safe_to_apply_from_time
locked_order_ids
frozen_future_order_ids
```

Runtime apply 时检查 generation，防止旧 plan 覆盖新 plan。

---

## 12. 配置新增建议

在 `backend/solver/ga_mmce/config.py` 中扩展 `GAConfig` 和 `DYNAMIC_GA_CONFIG`：

```text
# Archive
dynamic_archive_enabled = True
dynamic_archive_size = 60
dynamic_archive_seed_count = 24
dynamic_archive_update_top_k = 20
dynamic_warm_start_limit = 30
dynamic_warm_start_target_ratio = 0.70

# Fast incumbent
fast_incumbent_enabled = True
fast_incumbent_eval_count = 12
fast_incumbent_budget_seconds = 0.30

# Cache
distance_cache_enabled = True
distance_cache_size = 50000
candidate_cache_enabled = True
candidate_cache_size = 20000
cache_time_bucket_s = 10.0
cache_position_bucket_m = 20.0
cache_energy_bucket_wh = 10.0
final_strict_reevaluation = True

# Anytime
anytime_enabled = True
anytime_slice_seconds = 0.05
anytime_population_size = 12
anytime_max_generations_per_slice = 2

# Dynamic population mix
dynamic_random_seed_ratio = 0.20
dynamic_force_combined_b_seed = True
```

注意：第一阶段不要急着增大 `population_size`。更有效的是让已有 `population_size = 30` 中的 20 个以上来自 archive/warm starts。

---

## 13. 文件级修改清单

### 13.1 新增文件

```text
backend/solver/ga_mmce/archive.py
```

职责：

```text
GAArchive
ArchiveEntry
archive 去重/剪枝/多桶保留
```

可选新增：

```text
backend/solver/ga_mmce/cache.py
backend/solver/ga_mmce/decode_trace.py
```

第一版可以先把 LRU cache 放在 `physical_evaluator.py` 内，稳定后再抽出。

### 13.2 修改 solver.py

```text
1. __init__ 增加 dynamic_archive / last_population。
2. solve() 评估初始 population 后更新 archive。
3. 每代记录时更新 archive。
4. 返回前更新 archive。
5. 新增 _update_dynamic_archive()。
6. 新增 improve_archive_slice()。
7. 可选：solve() 支持 incumbent_plan / incumbent_individual。
```

### 13.3 修改 dynamic_rescheduler.py

```text
1. 在 previous_best warm start 前后加入 archive warm starts。
2. 新增 build_archive_warm_starts()。
3. 新增 adapt_individual_to_dynamic_orders()。
4. 新增 merge_warm_starts()。
5. 新增 _fast_incumbent_plan()。
6. final_plan 选择时纳入 fast incumbent。
7. summary/CSV 增加 archive/fast/cache 诊断字段。
```

### 13.4 修改 population.py

```text
1. 支持 dynamic_warm_start_target_ratio。
2. 动态模式下优先保留 archive/warm starts。
3. combined B seed 保底进入 population。
4. 控制 truck-only / random seed 比例。
```

### 13.5 修改 physical_evaluator.py

```text
1. 增加 distance cache。
2. 增加 candidate cache。
3. 包装 evaluate_fixed_mode_a/b/c。
4. 最终 best 支持 strict reevaluation。
5. 增加 cache hit/miss 诊断。
```

### 13.6 修改 config.py

增加 archive、fast incumbent、cache、anytime、dynamic population mix 配置字段。

### 13.7 修改 sim_engine.py 或 decision_engine.py

```text
1. 在无新单、无 dispatch 应用时调用 improve_archive_slice()。
2. 控制时间片预算。
3. 不直接应用 anytime plan，只更新 archive。
```

### 13.8 修改 runtime_adapter.py

第一阶段不必改。后续若 anytime plan 要自动替换 runtime route，需要加：

```text
plan_generation 检查
locked/running order 防覆盖
safe switch point 检查
```

---

## 14. 诊断字段建议

为确认滚动优化是否生效，建议在 GA summary 和 debug log 加：

```text
archive_enabled
archive_size_before
archive_size_after
archive_seed_count
archive_seed_accepted_count
archive_seed_rejected_count
archive_best_fitness
archive_b_heavy_count
archive_c_heavy_count

fast_incumbent_enabled
fast_incumbent_eval_count
fast_incumbent_found
fast_incumbent_fitness
fast_incumbent_selected

distance_cache_hit
distance_cache_miss
candidate_cache_hit
candidate_cache_miss

anytime_slice_used
anytime_slice_seconds
anytime_archive_improved

actual_generations
time_budget_hit
initial_best_modes
final_best_modes
```

这些字段可以写入：

```text
logs/ga_dynamic_replan.csv
backend/solver/ga_mmce_debug_log
plan.summary
```

---

## 15. 验证计划

### 15.1 单元级

```text
1. archive 去重和多桶保留。
2. archive individual 适配当前 order_ids/gene_pool。
3. fixed tail 强制不被 archive 覆盖。
4. distance cache key 稳定。
5. candidate cache 不跨错误 host/energy 状态复用。
```

### 15.2 脚本级

继续使用：

```text
python backend/scripts/test_ga_dynamic_rescheduler.py
```

重点观察：

```text
DYNAMIC_MODES 不应稳定退化为 {'A': all}
actual_generations = 0 时仍能有 B/C 或至少有 archive seed 参与
time_budget_hit = True 时 final plan 仍来自 fast incumbent 或 archive seed
```

### 15.3 日志级

检查：

```text
archive_seed_count > 0
archive_seed_accepted_count > 0
fast_incumbent_found = True
candidate_cache_hit 随多次动态重规划上升
distance_cache_hit 随多次动态重规划上升
```

### 15.4 回归风险

重点防：

```text
1. archive 个体引用旧 gene_pool 中已经不可用的无人机。
2. archive 个体改变 locked/running 订单。
3. candidate cache 用旧 _ga_host_type 或旧 energy 得出错误可行性。
4. anytime slice 意外修改 runtime 实体状态。
5. fast incumbent 可行性判断只看订单数量，不看 unserved / penalty。
```

---

## 16. 推荐实施顺序

### Step 1：Archive 复用

修改：

```text
archive.py
solver.py
dynamic_rescheduler.py
config.py
```

目标：

```text
动态重规划从 previous_best-only 变为 archive + previous_best。
```

### Step 2：Fast Incumbent

修改：

```text
dynamic_rescheduler.py
solver.py 可选
```

目标：

```text
0 代进化时也有历史可行解兜底。
```

### Step 3：Distance Cache

修改：

```text
physical_evaluator.py
```

目标：

```text
减少 road/uav distance 和 energy 重复计算。
```

### Step 4：Candidate Cache

修改：

```text
physical_evaluator.py
```

目标：

```text
减少 A/B/C 候选重复评估。
```

### Step 5：Anytime Slice

修改：

```text
solver.py
sim_engine.py 或 decision_engine.py
```

目标：

```text
没有新单时持续优化 archive。
```

### Step 6：Incremental Decode

修改：

```text
decoder.py
physical_evaluator.py
solver.py
archive.py
```

目标：

```text
只从变化点后重新 decode。
```

---

## 17. 最小可落地版本

如果只做一版最小但有效的改造，建议范围是：

```text
1. archive.py
2. solver.py 保存并更新 dynamic_archive
3. dynamic_rescheduler.py 从 archive 构造 warm starts
4. dynamic_rescheduler.py 增加 fast incumbent
5. config.py 增加 archive/fast 配置
```

这一版即可解决最核心问题：

```text
动态重规划不再只依赖 previous_best；
时间预算不足时，仍可从历史优秀种群中拿到 B/C 协同结构；
actual_generations = 0 不再天然等价于全 A。
```

随后再做 distance cache 和 candidate cache，用于解决计算成本问题。

