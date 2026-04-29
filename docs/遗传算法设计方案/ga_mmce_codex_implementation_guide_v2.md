# 基于三层染色体 GA 的 1 UGV + M UAV 协同配送算法实现指南（修正版）

## 0. 本版修改重点

本文件是在原 `ga_mmce_codex_implementation_guide.md` 基础上的修正版。核心修改是：

```text
旧设计：
    Chromosome 1：sequence
    Chromosome 2：assignment

新设计：
    Chromosome 1：sequence
    Chromosome 2：assignment
    Chromosome 3：rendezvous
```

其中：

```text
sequence   ：订单处理顺序
assignment ：每个订单由卡车 / 哪架无人机 / 哪种模式执行
rendezvous ：协同节点选择，即无人机从哪个仓库/充换电站起飞，送完后在哪个仓库/充换电站回收
```

本版最重要的原则是：

```text
GA 决定路径结构和协同节点。
GreedyMMCE 只复用底层物理计算能力，不复用完整贪心调度决策。
```

因此，GA Decoder **禁止直接调用**：

```python
GreedyMMCE.dispatch(...)
GreedyMMCE.dispatch_incremental(...)
GreedyMMCE.dispatch_replan_current_state(...)
GreedyMMCE._dispatch_impl(...)
GreedyMMCE._allocate_order(...)
GreedyMMCE._try_mode_b_with_waiting(...)
GreedyMMCE._try_mode_b(...)
GreedyMMCE._try_mode_c(...)
GreedyMMCE._try_mode_a(...)
GreedyMMCE._build_truck_route(...)
```

原因是这些函数会重新做：

```text
1. 订单 deadline 排序；
2. 模式 B_WAIT / B / C / A 贪心选择；
3. launch_station_id 贪心选择；
4. recovery_station_id 贪心选择；
5. 卡车最近邻路径构建；
6. 充换电站顺路插入。
```

如果 GA Decoder 调用这些函数，GA 的 `sequence`、`assignment` 和 `rendezvous` 会被贪心逻辑覆盖，GA 会退化成外层包装器。

---

# 1. 总体实现路线

当前任务不是重写环境、实体或前端，而是在已有项目基础上新增一个真正参与协同路径优化的 GA solver。

已有基础：

```text
1. greedy_mmce.py 已经实现完整贪心基线；
2. 环境与实体状态机已经实现；
3. 前端展示已经实现；
4. 订单、卡车、无人机、充换电站实体已经存在；
5. GA 只需要作为新的 solver 接入现有调度系统。
```

本版推荐架构：

```text
GA Individual
    sequence
    assignment
    rendezvous
        ↓
GA Decoder
    按 GA 指定顺序、模式、起飞点、回收点解码
        ↓
PhysicalEvaluator
    固定决策可行性评估
    复用 greedy_mmce 中的距离、能耗、OSM 路径、评分工具
        ↓
PlanBuilder
    生成 DispatchPlan，保持前端兼容
        ↓
Fitness
        ↓
GA selection / crossover / mutation
```

推荐新增目录：

```text
backend/solver/ga_mmce/
    __init__.py
    config.py
    chromosome.py
    operators.py
    population.py
    adapters.py
    physical_evaluator.py
    decoder.py
    fitness.py
    solver.py
    dynamic_rescheduler.py
```

与原版相比，新增核心文件：

```text
physical_evaluator.py
```

它负责固定起飞点、固定回收点、固定模式下的物理可行性评估。

---

# 2. 阶段划分

建议分四阶段推进。

```text
阶段 1：静态三层 GA 可跑通
    输入当前订单和环境状态
    GA 显式决定 sequence + assignment + rendezvous
    输出 DispatchPlan
    暂时不处理动态订单

阶段 2：接入 decision_engine.py
    通过配置选择 solver = ga_mmce
    前端无需改动
    GA 输出结构与 greedy_mmce.py 兼容

阶段 3：事件触发式动态重调度
    新订单到达时，冻结已完成和执行中任务
    对剩余订单 + 新订单重新 GA
    warm start 保留上一轮个体的未完成部分

阶段 4：局部搜索 / 修复增强
    对不可行 rendezvous 做局部修复
    可选加入 2-opt / station mutation / repair operator
```

---

# 3. Step 0：让 Codex 先阅读现有接口

## 3.1 目标

先不要修改代码，让 Codex 读取现有文件，确认：

```text
1. GreedyMMCE 的主入口；
2. DispatchPlan / AllocationResult / TruckRoute / DroneRoute 数据结构；
3. decision_engine.py 如何调用 solver；
4. 前端依赖哪些字段；
5. Order / Drone / Truck / SwapStation 的关键字段；
6. greedy_mmce.py 中哪些函数是贪心决策，哪些函数是底层物理工具。
```

## 3.2 必须区分的函数

### 3.2.1 GA 禁止调用的贪心决策函数

```python
GreedyMMCE.dispatch
GreedyMMCE.dispatch_incremental
GreedyMMCE.dispatch_replan_current_state
GreedyMMCE._dispatch_impl
GreedyMMCE._allocate_order
GreedyMMCE._try_mode_b_with_waiting
GreedyMMCE._try_mode_b
GreedyMMCE._try_mode_c
GreedyMMCE._try_mode_a
GreedyMMCE._build_truck_route
GreedyMMCE._check_energy_feasible
```

其中 `_check_energy_feasible` 也不建议直接用于 GA，因为它会在 recovery pool 中自动选择回收点；GA 需要的是“指定 recover_node 后只判断可不可行”。

### 3.2.2 GA 可以复用的底层物理/工具函数

```python
GreedyMMCE._load_road_graph
GreedyMMCE._road_dist
GreedyMMCE._dist
GreedyMMCE._flight_energy
GreedyMMCE._uav_energy_wh
GreedyMMCE._truck_energy_wh
GreedyMMCE._recalculate_plan_route_costs
GreedyMMCE.build_incremental_route_from_stops
```

特别注意：

```text
build_incremental_route_from_stops(truck, ordered_stops, current_time)
```

这个函数是按给定停靠顺序重建 OSM 路线，不会做最近邻重排，适合 GA Decoder 使用。

## 3.3 给 Codex 的提示词

```text
请先不要修改任何代码。

请阅读以下文件：
- backend/solver/greedy_mmce.py
- backend/solver/decision_engine.py
- backend/config/loader.py
- backend/core/entities/drone.py
- backend/core/entities/truck.py
- backend/core/entities/swap_station.py
- backend/core/entities/primitives.py

请输出：
1. greedy_mmce.py 的主调度入口函数名称、参数、返回值结构。
2. DispatchPlan、AllocationResult、TruckRoute、TruckRouteNode、DroneRoute 的字段。
3. decision_engine.py 是如何选择和调用 solver 的。
4. 前端依赖的调度结果字段有哪些。
5. Order / Drone / Truck / SwapStation 的关键字段名。
6. greedy_mmce.py 中哪些函数属于贪心决策层，GA Decoder 禁止调用。
7. greedy_mmce.py 中哪些函数属于物理工具层，GA 可以复用。
8. build_incremental_route_from_stops 是否可以按给定 ordered_stops 生成 OSM 路线。

只做代码阅读和总结，不要修改文件。
```

## 3.4 验收标准

Codex 应该输出一份接口总结，并明确：

```text
GA Decoder 不允许调用完整 greedy dispatch。
GA Decoder 只能调用物理工具层，或新建固定决策 physical_evaluator。
```

---

# 4. Step 1：新增 GA 配置文件

## 4.1 目标

让 GA 参数可配置，尤其新增第三层染色体的变异概率。

## 4.2 新增文件

```text
backend/solver/ga_mmce/config.py
```

## 4.3 推荐代码

```python
from dataclasses import dataclass


@dataclass
class GAConfig:
    population_size: int = 80
    generations: int = 120
    elite_ratio: float = 0.08
    tournament_k: int = 3

    crossover_rate: float = 0.9
    mutation_rate_sequence: float = 0.2
    mutation_rate_assignment: float = 0.15
    mutation_rate_rendezvous: float = 0.20

    use_greedy_seed: bool = True
    use_truck_only_seed: bool = True
    use_obl_seed: bool = True

    enable_repair: bool = True
    repair_penalty_factor: float = 1000.0

    big_m: float = 1e9

    weight_completion: float = 1.0
    weight_delay: float = 10.0
    weight_energy: float = 0.1
    weight_waiting: float = 5.0
    weight_infeasible: float = 100000.0
    weight_truck_distance: float = 1.0
    weight_uav_distance: float = 1.0

    max_runtime_seconds: float | None = None
    random_seed: int | None = 42
    verbose: bool = False

    # 是否允许 C 模式无人机送完后落在充换电站；若 False，则 C 必须回仓。
    allow_depot_drone_recover_at_station: bool = True

    # 若卡车与无人机回收同步失败，是否作为软惩罚而不是直接判死。
    soft_rendezvous_violation: bool = True

    truck_wait_max_s: float = 10.0
```

## 4.4 给 Codex 的提示词

```text
请新增 backend/solver/ga_mmce/config.py。

实现 GAConfig dataclass。

除了基础 GA 参数外，请新增：
- mutation_rate_rendezvous
- enable_repair
- repair_penalty_factor
- weight_truck_distance
- weight_uav_distance
- allow_depot_drone_recover_at_station
- soft_rendezvous_violation
- truck_wait_max_s

不要修改其他文件。
```

---

# 5. Step 2：设计三层染色体结构

## 5.1 目标

实现三层编码：

```text
Chromosome 1：sequence
Chromosome 2：assignment
Chromosome 3：rendezvous
```

含义：

```text
sequence[k]
    第 k 个被解码的订单 ID

assignment[k]
    该订单的模式与执行载具：A / B_<drone_id> / C_<drone_id>

rendezvous[k]
    对应订单的协同节点：
        A：None
        B：{"launch": <depot_or_station_id>, "recover": <depot_or_station_id>}
        C：{"launch": <depot_id>, "recover": <depot_or_station_id>}
```

## 5.2 新增文件

```text
backend/solver/ga_mmce/chromosome.py
```

## 5.3 推荐代码

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Rendezvous = dict[str, str] | None


@dataclass
class Individual:
    sequence: list[str]
    assignment: list[str]
    rendezvous: list[Rendezvous]
    fitness: float = float("inf")
    decoded_plan: Any | None = None
    penalties: dict[str, float] = field(default_factory=dict)

    def validate(self) -> None:
        n = len(self.sequence)
        if len(self.assignment) != n:
            raise ValueError("sequence and assignment length mismatch")
        if len(self.rendezvous) != n:
            raise ValueError("sequence and rendezvous length mismatch")
        if len(set(self.sequence)) != n:
            raise ValueError("sequence contains duplicated order ids")

        for gene, rv in zip(self.assignment, self.rendezvous):
            if gene == "A":
                if rv is not None:
                    raise ValueError("A mode must use rendezvous=None")
            elif gene.startswith("B_"):
                if not isinstance(rv, dict):
                    raise ValueError("B mode must use rendezvous dict")
                if not rv.get("launch") or not rv.get("recover"):
                    raise ValueError("B mode rendezvous must include launch and recover")
            elif gene.startswith("C_"):
                if not isinstance(rv, dict):
                    raise ValueError("C mode must use rendezvous dict")
                if rv.get("launch") != "DEPOT" and not rv.get("launch", "").startswith("depot"):
                    raise ValueError("C mode launch must be DEPOT/depot id")
                if not rv.get("recover"):
                    raise ValueError("C mode rendezvous must include recover")
            else:
                raise ValueError(f"unknown assignment gene: {gene}")


def make_gene_pool(drone_ids: list[str | int]) -> list[str]:
    genes = ["A"]
    for uid in drone_ids:
        genes.append(f"B_{uid}")
        genes.append(f"C_{uid}")
    return genes


def make_node_pool(depot_ids: list[str], station_ids: list[str]) -> list[str]:
    return list(depot_ids) + list(station_ids)


def normalize_depot_id(depot_ids: list[str]) -> str:
    if depot_ids:
        return depot_ids[0]
    return "DEPOT"
```

## 5.4 给 Codex 的提示词

```text
请修改/新增 backend/solver/ga_mmce/chromosome.py。

实现三层染色体 Individual：
- sequence: list[str]
- assignment: list[str]
- rendezvous: list[dict[str, str] | None]
- fitness
- decoded_plan
- penalties

validate() 必须检查：
1. 三条染色体长度一致；
2. sequence 不重复；
3. A 的 rendezvous 必须为 None；
4. B_<uid> 的 rendezvous 必须包含 launch 和 recover；
5. C_<uid> 的 launch 必须是 DEPOT 或 depot id，recover 必须存在；
6. 不允许未知 gene。

同时实现：
- make_gene_pool(drone_ids)
- make_node_pool(depot_ids, station_ids)
- normalize_depot_id(depot_ids)
```

---

# 6. Step 3：实现三层染色体交叉、变异、选择算子

## 6.1 目标

实现：

```text
1. sequence 的 OX 顺序交叉；
2. assignment 按订单 ID 对齐继承；
3. rendezvous 按订单 ID 对齐继承；
4. sequence swap mutation；
5. assignment mutation；
6. rendezvous mutation；
7. tournament selection。
```

## 6.2 新增文件

```text
backend/solver/ga_mmce/operators.py
```

## 6.3 推荐代码

```python
from __future__ import annotations

import copy
import random

from .chromosome import Individual, normalize_depot_id


def make_random_rendezvous_for_gene(
    gene: str,
    depot_ids: list[str],
    station_ids: list[str],
    allow_c_recover_station: bool = True,
):
    depot_id = normalize_depot_id(depot_ids)
    legal_nodes = [depot_id] + list(station_ids)

    if gene == "A":
        return None

    if gene.startswith("B_"):
        return {
            "launch": random.choice(legal_nodes),
            "recover": random.choice(legal_nodes),
        }

    if gene.startswith("C_"):
        recover_pool = legal_nodes if allow_c_recover_station else [depot_id]
        return {
            "launch": depot_id,
            "recover": random.choice(recover_pool),
        }

    raise ValueError(f"unknown gene: {gene}")


def order_crossover(p1: Individual, p2: Individual) -> tuple[Individual, Individual]:
    n = len(p1.sequence)
    if n <= 1:
        return copy.deepcopy(p1), copy.deepcopy(p2)

    a, b = sorted(random.sample(range(n), 2))

    def build_child(base: Individual, donor: Individual) -> Individual:
        child_seq = [None] * n
        child_seq[a:b + 1] = base.sequence[a:b + 1]

        donor_fill = [oid for oid in donor.sequence if oid not in child_seq]
        fill_idx = 0
        for i in range(n):
            if child_seq[i] is None:
                child_seq[i] = donor_fill[fill_idx]
                fill_idx += 1

        base_map = {
            oid: (gene, rv)
            for oid, gene, rv in zip(base.sequence, base.assignment, base.rendezvous)
        }
        donor_map = {
            oid: (gene, rv)
            for oid, gene, rv in zip(donor.sequence, donor.assignment, donor.rendezvous)
        }

        child_assignment = []
        child_rendezvous = []
        for oid in child_seq:
            gene, rv = base_map[oid] if random.random() < 0.5 else donor_map[oid]
            child_assignment.append(copy.deepcopy(gene))
            child_rendezvous.append(copy.deepcopy(rv))

        return Individual(
            sequence=list(child_seq),
            assignment=child_assignment,
            rendezvous=child_rendezvous,
        )

    return build_child(p1, p2), build_child(p2, p1)


def mutate(
    ind: Individual,
    gene_pool: list[str],
    depot_ids: list[str],
    station_ids: list[str],
    p_seq: float,
    p_assign: float,
    p_rendezvous: float,
    allow_c_recover_station: bool = True,
) -> None:
    n = len(ind.sequence)

    # 1. 任务顺序交换变异
    if n >= 2 and random.random() < p_seq:
        i, j = random.sample(range(n), 2)
        ind.sequence[i], ind.sequence[j] = ind.sequence[j], ind.sequence[i]
        ind.assignment[i], ind.assignment[j] = ind.assignment[j], ind.assignment[i]
        ind.rendezvous[i], ind.rendezvous[j] = ind.rendezvous[j], ind.rendezvous[i]

    # 2. 模式/载具变异
    for i in range(n):
        if random.random() < p_assign:
            new_gene = random.choice(gene_pool)
            ind.assignment[i] = new_gene
            ind.rendezvous[i] = make_random_rendezvous_for_gene(
                new_gene,
                depot_ids,
                station_ids,
                allow_c_recover_station,
            )

    # 3. 协同节点变异
    for i in range(n):
        if random.random() < p_rendezvous:
            ind.rendezvous[i] = make_random_rendezvous_for_gene(
                ind.assignment[i],
                depot_ids,
                station_ids,
                allow_c_recover_station,
            )


def tournament_select(population: list[Individual], k: int) -> Individual:
    candidates = random.sample(population, min(k, len(population)))
    candidates.sort(key=lambda x: x.fitness)
    return copy.deepcopy(candidates[0])
```

## 6.4 给 Codex 的提示词

```text
请新增/修改 backend/solver/ga_mmce/operators.py。

实现：
1. make_random_rendezvous_for_gene(...)
2. order_crossover(p1, p2)
3. mutate(...)
4. tournament_select(...)

关键要求：
- sequence 使用 OX 交叉。
- assignment 和 rendezvous 必须按订单 id 对齐继承。
- 不能简单按下标复制 assignment/rendezvous。
- sequence swap mutation 时，三条染色体对应位置必须一起交换。
- assignment mutation 后，必须同步重建该订单的 rendezvous。
- rendezvous mutation 只改变 launch/recover，不改变订单和载具。
```

---

# 7. Step 4：实现种群初始化

## 7.1 目标

初始种群必须生成合法的三层染色体。

包括：

```text
1. 随机解；
2. 纯卡车解；
3. greedy seed 转换而来的三层个体；
4. OBL 反向学习个体。
```

## 7.2 新增文件

```text
backend/solver/ga_mmce/population.py
```

## 7.3 推荐代码

```python
from __future__ import annotations

import copy
import random

from .chromosome import Individual, make_gene_pool
from .operators import make_random_rendezvous_for_gene


def make_random_individual(
    order_ids: list[str],
    gene_pool: list[str],
    depot_ids: list[str],
    station_ids: list[str],
    allow_c_recover_station: bool = True,
) -> Individual:
    seq = list(order_ids)
    random.shuffle(seq)
    assignment = []
    rendezvous = []

    for _ in seq:
        gene = random.choice(gene_pool)
        assignment.append(gene)
        rendezvous.append(
            make_random_rendezvous_for_gene(
                gene,
                depot_ids,
                station_ids,
                allow_c_recover_station,
            )
        )

    return Individual(seq, assignment, rendezvous)


def make_truck_only_individual(order_ids: list[str]) -> Individual:
    return Individual(
        sequence=list(order_ids),
        assignment=["A"] * len(order_ids),
        rendezvous=[None] * len(order_ids),
    )


def make_obl_individual(
    base: Individual,
    gene_pool: list[str],
    depot_ids: list[str],
    station_ids: list[str],
    allow_c_recover_station: bool = True,
) -> Individual:
    seq = list(reversed(base.sequence))
    assignment = []
    rendezvous = []

    for gene in reversed(base.assignment):
        if gene == "A":
            new_gene = random.choice(gene_pool)
        elif gene.startswith("B_"):
            uid = gene.split("_", 1)[1]
            new_gene = f"C_{uid}" if f"C_{uid}" in gene_pool else "A"
        elif gene.startswith("C_"):
            uid = gene.split("_", 1)[1]
            new_gene = f"B_{uid}" if f"B_{uid}" in gene_pool else "A"
        else:
            new_gene = random.choice(gene_pool)

        assignment.append(new_gene)
        rendezvous.append(
            make_random_rendezvous_for_gene(
                new_gene,
                depot_ids,
                station_ids,
                allow_c_recover_station,
            )
        )

    return Individual(seq, assignment, rendezvous)


def initialize_population(
    order_ids: list[str],
    drone_ids: list[str | int],
    depot_ids: list[str],
    station_ids: list[str],
    pop_size: int,
    greedy_seed: Individual | None = None,
    warm_start: list[Individual] | None = None,
    use_truck_only_seed: bool = True,
    use_obl_seed: bool = True,
    allow_c_recover_station: bool = True,
) -> list[Individual]:
    gene_pool = make_gene_pool(drone_ids)
    population: list[Individual] = []

    if warm_start:
        population.extend(copy.deepcopy(warm_start))

    if greedy_seed is not None:
        population.append(copy.deepcopy(greedy_seed))

    if use_truck_only_seed:
        population.append(make_truck_only_individual(order_ids))

    if use_obl_seed and population:
        population.append(
            make_obl_individual(
                population[0],
                gene_pool,
                depot_ids,
                station_ids,
                allow_c_recover_station,
            )
        )

    while len(population) < pop_size:
        population.append(
            make_random_individual(
                order_ids,
                gene_pool,
                depot_ids,
                station_ids,
                allow_c_recover_station,
            )
        )

    return population[:pop_size]
```

## 7.4 给 Codex 的提示词

```text
请新增/修改 backend/solver/ga_mmce/population.py。

要求：
1. 所有初始化个体都必须包含 sequence、assignment、rendezvous 三层。
2. A 模式 rendezvous=None。
3. B 模式随机生成 launch/recover。
4. C 模式 launch 固定为 depot，recover 根据配置可为 depot 或 station。
5. initialize_population 支持 greedy_seed 和 warm_start。
6. 不要访问真实环境状态，只使用传入的 order_ids、drone_ids、depot_ids、station_ids。
```

---

# 8. Step 5：写 GA 与现有环境之间的适配层

## 8.1 目标

GA 模块不应该直接猜实体字段名。需要 adapter 把现有系统状态转换成 GA 需要的简单列表。

## 8.2 新增文件

```text
backend/solver/ga_mmce/adapters.py
```

## 8.3 需要实现的函数

```python
def extract_active_order_ids(state) -> list[str]:
    """返回需要进入 GA 的订单 ID，排除 completed / cancelled / locked。"""


def extract_drone_ids(state) -> list[str]:
    """返回可被 GA 分配的无人机 ID。"""


def extract_truck_ids(state) -> list[str]:
    """返回可用卡车 ID。本问题通常只有一辆。"""


def extract_depot_ids(state) -> list[str]:
    """返回仓库 ID。"""


def extract_station_ids(state) -> list[str]:
    """返回充换电站 ID。"""


def clone_state_for_decode(state):
    """返回 state 深拷贝，确保 decode 不污染真实环境。"""


def greedy_plan_to_individual(greedy_plan, order_ids, drone_ids, depot_ids, station_ids):
    """
    将 greedy plan 转换成三层 Individual。
    可从 AllocationResult 中读取：mode、drone_id、launch_station_id、recovery_station_id。
    若字段缺失，则用合法随机 rendezvous 修复。
    """
```

## 8.4 greedy_plan_to_individual 规则

```text
alloc.mode == "A":
    assignment = "A"
    rendezvous = None

alloc.mode in ("B", "B_WAIT", "B_DYNAMIC"):
    assignment = f"B_{alloc.drone_id}"
    rendezvous = {
        "launch": alloc.launch_station_id or depot_id_or_best_guess,
        "recover": alloc.recovery_station_id,
    }

alloc.mode == "C":
    assignment = f"C_{alloc.drone_id}"
    rendezvous = {
        "launch": depot_id,
        "recover": alloc.recovery_station_id or depot_id,
    }
```

## 8.5 给 Codex 的提示词

```text
请新增/修改 backend/solver/ga_mmce/adapters.py。

实现：
1. extract_active_order_ids(state)
2. extract_drone_ids(state)
3. extract_truck_ids(state)
4. extract_depot_ids(state)
5. extract_station_ids(state)
6. clone_state_for_decode(state)
7. greedy_plan_to_individual(greedy_plan, order_ids, drone_ids, depot_ids, station_ids)

要求：
- 不要修改实体类。
- clone_state_for_decode 优先使用 copy.deepcopy。
- greedy_plan_to_individual 必须返回三层 Individual。
- 如果 greedy plan 中没有 launch_station_id，则用 depot 或最近合法站点作为 fallback。
- 如果无法解析，则返回 None，并添加 TODO 注释。
```

---

# 9. Step 6：实现 PhysicalEvaluator，固定协同节点评估

## 9.1 目标

新增 GA 专用物理评估器。

它的职责是：

```text
输入 GA 已经指定的：
    order_id
    mode
    drone_id
    truck_id
    launch_node_id
    recover_node_id

输出：
    这个固定动作是否可行
    距离、能耗、时间、迟到、等待、惩罚
    需要追加的 truck stop
    需要追加的 drone route
```

它不允许自动搜索最优起飞点或最优回收点。

## 9.2 新增文件

```text
backend/solver/ga_mmce/physical_evaluator.py
```

## 9.3 Candidate 数据结构

```python
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GACandidate:
    order_id: str
    mode: str
    feasible: bool
    reason: str = ""

    truck_id: str = ""
    drone_id: str = ""
    launch_node_id: str = ""
    recover_node_id: str = ""

    completion_time: float = 0.0
    truck_distance: float = 0.0
    uav_distance: float = 0.0
    truck_energy: float = 0.0
    uav_energy: float = 0.0
    waiting_time: float = 0.0
    lateness: float = 0.0

    cost_dist: float = 0.0
    cost_energy: float = 0.0
    cost_penalty: float = 0.0
    score_total: float = math.inf

    truck_stops: list[dict[str, Any]] = field(default_factory=list)
    drone_route_fragment: Any | None = None
    allocation_fragment: Any | None = None
```

## 9.4 PhysicalEvaluator 类骨架

```python
class PhysicalEvaluator:
    def __init__(self, entity_mgr, greedy_helper, config):
        self.entity_mgr = entity_mgr
        self.greedy = greedy_helper
        self.config = config

    def get_node_position(self, node_id: str):
        if node_id in self.entity_mgr.depots:
            return self.entity_mgr.depots[node_id].location
        if node_id in self.entity_mgr.stations:
            return self.entity_mgr.stations[node_id].location
        raise KeyError(f"unknown depot/station node_id: {node_id}")

    def is_legal_rendezvous_node(self, node_id: str) -> bool:
        return node_id in self.entity_mgr.depots or node_id in self.entity_mgr.stations

    def check_fixed_recovery_energy(self, drone, launch_pos, delivery_pos, recover_pos, payload):
        e1 = self.greedy._flight_energy(drone, launch_pos, delivery_pos, payload)
        e2 = self.greedy._flight_energy(drone, delivery_pos, recover_pos, 0.0)
        need = (e1 + e2) * self.greedy.ENERGY_SAFETY_FACTOR
        if need > drone.battery_current:
            return False, need, "energy_not_enough"
        return True, need, ""
```

## 9.5 固定模式 A 评估

```python
    def evaluate_fixed_mode_a(self, state, order_id: str, truck_id: str) -> GACandidate:
        order = state.orders[order_id] if hasattr(state, "orders") else self.entity_mgr.orders[order_id]
        truck = self.entity_mgr.trucks.get(truck_id)
        if truck is None:
            return GACandidate(order_id, "A", False, reason="truck_not_found")

        truck_pos = truck.get_location(state.current_time) if hasattr(state, "current_time") else truck.get_location(0.0)
        dist = self.greedy._road_dist(truck_pos, order.delivery_loc)
        energy = self.greedy._truck_energy_wh(dist)
        speed = max(1e-6, float(getattr(truck, "speed", 0.0)))
        arrival = (getattr(state, "current_time", 0.0) + dist / speed)
        completion = arrival + self.greedy.SERVICE_TIME_CUSTOMER
        lateness = max(0.0, completion - order.deadline)

        candidate = GACandidate(
            order_id=order_id,
            mode="A",
            feasible=True,
            truck_id=truck_id,
            completion_time=completion,
            truck_distance=dist,
            truck_energy=energy,
            lateness=lateness,
            truck_stops=[{
                "node_id": order_id,
                "node_type": "customer",
                "position": order.delivery_loc,
                "order_id": order_id,
            }],
        )
        return self.score_candidate(candidate, order)
```

## 9.6 固定模式 B 评估

固定模式 B 不允许自动选站点。

```python
    def evaluate_fixed_mode_b(
        self,
        state,
        order_id: str,
        truck_id: str,
        drone_id: str,
        launch_node_id: str,
        recover_node_id: str,
    ) -> GACandidate:
        order = state.orders[order_id] if hasattr(state, "orders") else self.entity_mgr.orders[order_id]
        truck = self.entity_mgr.trucks.get(truck_id)
        drone = self.entity_mgr.drones.get(drone_id)

        if truck is None:
            return GACandidate(order_id, "B", False, reason="truck_not_found")
        if drone is None:
            return GACandidate(order_id, "B", False, reason="drone_not_found")
        if not self.is_legal_rendezvous_node(launch_node_id):
            return GACandidate(order_id, "B", False, reason="illegal_launch_node")
        if not self.is_legal_rendezvous_node(recover_node_id):
            return GACandidate(order_id, "B", False, reason="illegal_recover_node")
        if order.payload_weight > drone.payload_capacity:
            return GACandidate(order_id, "B", False, reason="payload_exceed")

        launch_pos = self.get_node_position(launch_node_id)
        recover_pos = self.get_node_position(recover_node_id)

        ok, energy_need, reason = self.check_fixed_recovery_energy(
            drone,
            launch_pos,
            order.delivery_loc,
            recover_pos,
            order.payload_weight,
        )
        if not ok:
            return GACandidate(order_id, "B", False, reason=reason)

        uav_dist_out = self.greedy._dist(launch_pos, order.delivery_loc)
        uav_dist_back = self.greedy._dist(order.delivery_loc, recover_pos)
        uav_distance = uav_dist_out + uav_dist_back
        uav_energy = self.greedy._uav_energy_wh(drone, launch_pos, order.delivery_loc, order.payload_weight)
        uav_energy += self.greedy._uav_energy_wh(drone, order.delivery_loc, recover_pos, 0.0)

        truck_pos = truck.get_location(getattr(state, "current_time", 0.0))
        truck_dist_to_launch = self.greedy._road_dist(truck_pos, launch_pos)
        truck_dist_launch_to_recover = self.greedy._road_dist(launch_pos, recover_pos)
        truck_distance = truck_dist_to_launch + truck_dist_launch_to_recover
        truck_energy = self.greedy._truck_energy_wh(truck_distance)

        current_time = getattr(state, "current_time", 0.0)
        truck_speed = max(1e-6, float(getattr(truck, "speed", 0.0)))
        launch_arrival_time = current_time + truck_dist_to_launch / truck_speed
        launch_time = launch_arrival_time + self.greedy.TRUCK_DRONE_LAUNCH_TIME
        delivery_time = launch_time + uav_dist_out / drone.cruise_speed + self.greedy.delivery_service_time
        drone_recover_arrival = delivery_time + uav_dist_back / drone.cruise_speed
        truck_recover_arrival = launch_arrival_time + truck_dist_launch_to_recover / truck_speed

        truck_wait = max(0.0, drone_recover_arrival - truck_recover_arrival)
        if truck_wait > self.config.truck_wait_max_s and not self.config.soft_rendezvous_violation:
            return GACandidate(order_id, "B", False, reason="rendezvous_wait_timeout")

        lateness = max(0.0, delivery_time - order.deadline)

        candidate = GACandidate(
            order_id=order_id,
            mode="B",
            feasible=True,
            truck_id=truck_id,
            drone_id=drone_id,
            launch_node_id=launch_node_id,
            recover_node_id=recover_node_id,
            completion_time=delivery_time,
            truck_distance=truck_distance,
            uav_distance=uav_distance,
            truck_energy=truck_energy,
            uav_energy=uav_energy,
            waiting_time=truck_wait,
            lateness=lateness,
            truck_stops=[
                {
                    "node_id": launch_node_id,
                    "node_type": "recovery",
                    "position": launch_pos,
                    "order_id": "",
                    "action": "launch",
                },
                {
                    "node_id": recover_node_id,
                    "node_type": "recovery",
                    "position": recover_pos,
                    "order_id": "",
                    "action": "recover",
                },
            ],
        )
        return self.score_candidate(candidate, order)
```

## 9.7 固定模式 C 评估

```python
    def evaluate_fixed_mode_c(
        self,
        state,
        order_id: str,
        drone_id: str,
        recover_node_id: str,
    ) -> GACandidate:
        order = state.orders[order_id] if hasattr(state, "orders") else self.entity_mgr.orders[order_id]
        drone = self.entity_mgr.drones.get(drone_id)
        if drone is None:
            return GACandidate(order_id, "C", False, reason="drone_not_found")
        if order.payload_weight > drone.payload_capacity:
            return GACandidate(order_id, "C", False, reason="payload_exceed")
        if not self.is_legal_rendezvous_node(recover_node_id):
            return GACandidate(order_id, "C", False, reason="illegal_recover_node")

        depots = list(self.entity_mgr.depots.values())
        if not depots:
            return GACandidate(order_id, "C", False, reason="depot_not_found")
        depot = depots[0]
        recover_pos = self.get_node_position(recover_node_id)

        ok, energy_need, reason = self.check_fixed_recovery_energy(
            drone,
            depot.location,
            order.delivery_loc,
            recover_pos,
            order.payload_weight,
        )
        if not ok:
            return GACandidate(order_id, "C", False, reason=reason)

        dist_out = self.greedy._dist(depot.location, order.delivery_loc)
        dist_back = self.greedy._dist(order.delivery_loc, recover_pos)
        uav_distance = dist_out + dist_back
        uav_energy = self.greedy._uav_energy_wh(drone, depot.location, order.delivery_loc, order.payload_weight)
        uav_energy += self.greedy._uav_energy_wh(drone, order.delivery_loc, recover_pos, 0.0)

        current_time = getattr(state, "current_time", 0.0)
        delivery_time = current_time + dist_out / drone.cruise_speed + self.greedy.delivery_service_time
        lateness = max(0.0, delivery_time - order.deadline)

        candidate = GACandidate(
            order_id=order_id,
            mode="C",
            feasible=True,
            truck_id="",
            drone_id=drone_id,
            launch_node_id=depot.depot_id,
            recover_node_id=recover_node_id,
            completion_time=delivery_time,
            uav_distance=uav_distance,
            uav_energy=uav_energy,
            lateness=lateness,
            truck_stops=[],
        )
        return self.score_candidate(candidate, order)
```

## 9.8 评分函数

```python
    def score_candidate(self, candidate: GACandidate, order) -> GACandidate:
        if not candidate.feasible:
            candidate.score_total = self.config.big_m
            return candidate

        candidate.cost_dist = (
            self.config.weight_truck_distance * candidate.truck_distance
            + self.config.weight_uav_distance * candidate.uav_distance
        )
        candidate.cost_energy = self.config.weight_energy * (
            candidate.truck_energy + candidate.uav_energy
        )
        candidate.cost_penalty = (
            self.config.weight_delay * candidate.lateness
            + self.config.weight_waiting * candidate.waiting_time
        )
        candidate.score_total = candidate.cost_dist + candidate.cost_energy + candidate.cost_penalty
        return candidate
```

## 9.9 给 Codex 的提示词

```text
请新增 backend/solver/ga_mmce/physical_evaluator.py。

实现：
1. GACandidate dataclass。
2. PhysicalEvaluator 类。
3. get_node_position(node_id)。
4. is_legal_rendezvous_node(node_id)。
5. check_fixed_recovery_energy(...)
   - 输入 launch_pos、delivery_pos、recover_pos。
   - 只判断 GA 指定 recover_node 是否可行。
   - 不允许自动搜索最近回收点。
6. evaluate_fixed_mode_a(state, order_id, truck_id)。
7. evaluate_fixed_mode_b(state, order_id, truck_id, drone_id, launch_node_id, recover_node_id)。
8. evaluate_fixed_mode_c(state, order_id, drone_id, recover_node_id)。
9. score_candidate(candidate, order)。

PhysicalEvaluator 可以复用 GreedyMMCE 的底层函数：
- _road_dist
- _dist
- _flight_energy
- _uav_energy_wh
- _truck_energy_wh
- build_incremental_route_from_stops

但禁止调用：
- dispatch
- dispatch_incremental
- dispatch_replan_current_state
- _dispatch_impl
- _allocate_order
- _try_mode_b_with_waiting
- _try_mode_b
- _try_mode_c
- _try_mode_a
- _build_truck_route
- _check_energy_feasible
```

---

# 10. Step 7：实现 Decoder，按 GA 决策生成 DispatchPlan

## 10.1 目标

Decoder 读取：

```text
individual.sequence
individual.assignment
individual.rendezvous
```

按 GA 指定顺序逐个订单解码，不再让 greedy 重排顺序、重选模式或重选回收点。

## 10.2 新增文件

```text
backend/solver/ga_mmce/decoder.py
```

## 10.3 Decoder 结构

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .chromosome import Individual
from .config import GAConfig
from .adapters import clone_state_for_decode
from .physical_evaluator import PhysicalEvaluator


@dataclass
class DecodeResult:
    plan: Any
    objective: float
    penalties: dict[str, float]
    feasible: bool


class GADecoder:
    def __init__(self, config: GAConfig, evaluator: PhysicalEvaluator):
        self.config = config
        self.evaluator = evaluator

    def decode(self, individual: Individual, state) -> DecodeResult:
        individual.validate()
        state_copy = clone_state_for_decode(state)

        penalties: dict[str, float] = {}
        allocations = []
        drone_routes = {}
        truck_ordered_stops: list[dict] = []
        total_score = 0.0
        feasible = True

        truck_id = self._select_default_truck_id(state_copy)

        for order_id, gene, rv in zip(
            individual.sequence,
            individual.assignment,
            individual.rendezvous,
        ):
            if gene == "A":
                candidate = self.evaluator.evaluate_fixed_mode_a(
                    state_copy,
                    order_id=order_id,
                    truck_id=truck_id,
                )

            elif gene.startswith("B_"):
                drone_id = gene.split("_", 1)[1]
                candidate = self.evaluator.evaluate_fixed_mode_b(
                    state_copy,
                    order_id=order_id,
                    truck_id=truck_id,
                    drone_id=drone_id,
                    launch_node_id=rv["launch"],
                    recover_node_id=rv["recover"],
                )

            elif gene.startswith("C_"):
                drone_id = gene.split("_", 1)[1]
                candidate = self.evaluator.evaluate_fixed_mode_c(
                    state_copy,
                    order_id=order_id,
                    drone_id=drone_id,
                    recover_node_id=rv["recover"],
                )

            else:
                candidate = None

            if candidate is None or not candidate.feasible:
                feasible = False
                reason = candidate.reason if candidate else "unknown_gene"
                penalties[reason] = penalties.get(reason, 0.0) + 1.0
                total_score += self.config.big_m
                continue

            # 只追加 GA 指定产生的节点，不做最近邻重排。
            truck_ordered_stops.extend(candidate.truck_stops)
            allocations.append(candidate.allocation_fragment or candidate)
            if candidate.drone_route_fragment is not None:
                drone_routes[candidate.drone_id] = candidate.drone_route_fragment
            total_score += candidate.score_total

            self._apply_candidate_state_update(state_copy, candidate)

        truck_routes = self._build_truck_routes_by_given_order(
            state_copy,
            truck_id,
            truck_ordered_stops,
        )

        plan = self._build_dispatch_plan(
            allocations=allocations,
            truck_routes=truck_routes,
            drone_routes=drone_routes,
            cost_total=total_score,
            penalties=penalties,
        )

        return DecodeResult(
            plan=plan,
            objective=total_score + self._penalty_cost(penalties),
            penalties=penalties,
            feasible=feasible,
        )
```

## 10.4 核心要求

### 10.4.1 不允许 Decoder 调用完整 greedy

禁止：

```python
greedy.dispatch(...)
greedy._dispatch_impl(...)
greedy._allocate_order(...)
greedy._build_truck_route(...)
```

### 10.4.2 卡车路径顺序由 GA 决定

Decoder 根据 `sequence + assignment + rendezvous` 生成：

```python
truck_ordered_stops = [
    {"node_id": "order_1", "node_type": "customer", ...},
    {"node_id": "S1", "node_type": "recovery", "action": "launch", ...},
    {"node_id": "S3", "node_type": "recovery", "action": "recover", ...},
]
```

然后调用：

```python
greedy.build_incremental_route_from_stops(
    truck=truck,
    ordered_stops=truck_ordered_stops,
    current_time=current_time,
)
```

这个函数按给定顺序构建 OSM 路径，不会像 `_build_truck_route` 那样最近邻重排。

## 10.5 apply_candidate 状态更新原则

`_apply_candidate_state_update` 应做最小必要更新：

```text
A 模式：
    更新卡车虚拟位置、时间、库存；
    标记订单完成。

B 模式：
    更新卡车到 launch/recover 的虚拟时间；
    更新无人机电量、位置、时间；
    如果 recover 是充换电站，处理换电或恢复满电；
    标记订单完成。

C 模式：
    更新无人机位置、时间、电量；
    标记订单完成。
```

如果真实实体状态机复杂，第一版可以在 decode 内维护虚拟状态字典，不直接改实体字段。

## 10.6 给 Codex 的提示词

```text
请新增/修改 backend/solver/ga_mmce/decoder.py。

实现 GADecoder 和 DecodeResult。

关键要求：
1. decode 必须读取三层染色体：sequence + assignment + rendezvous。
2. decode 必须在 state 深拷贝上执行。
3. decode 按 GA sequence 的顺序处理订单，不允许重新按 deadline 排序。
4. decode 按 GA assignment 指定模式，不允许重新选择 B/C/A。
5. decode 按 GA rendezvous 指定 launch/recover，不允许自动搜索最近站点。
6. B 模式调用 evaluate_fixed_mode_b。
7. C 模式调用 evaluate_fixed_mode_c。
8. A 模式调用 evaluate_fixed_mode_a。
9. 卡车 ordered_stops 的顺序由 GA 解码过程产生。
10. 构建卡车路线时调用 build_incremental_route_from_stops，而不是 _build_truck_route。
11. 输出 DispatchPlan 结构必须兼容前端。
12. 不可行时记录 penalty，不能静默跳过。
```

---

# 11. Step 8：实现 Fitness 计算

## 11.1 目标

适应度统一在 `fitness.py` 中处理。

## 11.2 新增文件

```text
backend/solver/ga_mmce/fitness.py
```

## 11.3 推荐结构

```python
def compute_fitness(plan, penalties, config):
    """
    objective =
        completion_cost
        + delay_cost
        + energy_cost
        + waiting_cost
        + infeasible_penalty
    """
```

如果 `DecodeResult.objective` 已经包含候选评分，也可以只追加不可行惩罚。

## 11.4 给 Codex 的提示词

```text
请新增 backend/solver/ga_mmce/fitness.py。

实现 compute_fitness(plan, penalties, config)。

要求：
1. fitness 越小越好。
2. 至少包含：
   - completion time cost
   - delay penalty
   - energy cost
   - waiting penalty
   - infeasible penalty
3. 对缺失字段安全处理。
4. 不要依赖 greedy 的完整 dispatch 结果。
5. 返回 float。
```

---

# 12. Step 9：实现 GA 主求解器

## 12.1 新增文件

```text
backend/solver/ga_mmce/solver.py
```

## 12.2 核心职责

```text
1. 从环境提取 active order ids；
2. 提取 drone ids / depot ids / station ids / truck ids；
3. 初始化三层种群；
4. 可选使用 greedy seed，但 greedy seed 只用于初始化个体；
5. 循环迭代；
6. 返回最佳 DispatchPlan。
```

## 12.3 推荐代码框架

```python
from __future__ import annotations

import copy
import logging
import random
import time

from greedy_mmce import GreedyMMCE

from .adapters import (
    extract_active_order_ids,
    extract_drone_ids,
    extract_truck_ids,
    extract_depot_ids,
    extract_station_ids,
    greedy_plan_to_individual,
)
from .chromosome import make_gene_pool
from .config import GAConfig
from .decoder import GADecoder
from .operators import order_crossover, mutate, tournament_select
from .physical_evaluator import PhysicalEvaluator
from .population import initialize_population

logger = logging.getLogger(__name__)


class GAMMCESolver:
    def __init__(self, entity_mgr, config: GAConfig | None = None):
        self.entity_mgr = entity_mgr
        self.config = config or GAConfig()
        self.greedy_helper = GreedyMMCE(entity_mgr)
        self.evaluator = PhysicalEvaluator(entity_mgr, self.greedy_helper, self.config)
        self.decoder = GADecoder(self.config, self.evaluator)
        self.last_best_individual = None

        if self.config.random_seed is not None:
            random.seed(self.config.random_seed)

    def solve(self, state, warm_start=None):
        started = time.time()

        order_ids = extract_active_order_ids(state)
        drone_ids = extract_drone_ids(state)
        truck_ids = extract_truck_ids(state)
        depot_ids = extract_depot_ids(state)
        station_ids = extract_station_ids(state)
        gene_pool = make_gene_pool(drone_ids)

        if not order_ids:
            return self._empty_plan()

        greedy_seed = None
        if self.config.use_greedy_seed:
            # 注意：greedy 只作为 seed，不能作为 decoder。
            greedy_plan = self._make_greedy_seed_plan(state)
            greedy_seed = greedy_plan_to_individual(
                greedy_plan,
                order_ids,
                drone_ids,
                depot_ids,
                station_ids,
            )

        population = initialize_population(
            order_ids=order_ids,
            drone_ids=drone_ids,
            depot_ids=depot_ids,
            station_ids=station_ids,
            pop_size=self.config.population_size,
            greedy_seed=greedy_seed,
            warm_start=warm_start,
            use_truck_only_seed=self.config.use_truck_only_seed,
            use_obl_seed=self.config.use_obl_seed,
            allow_c_recover_station=self.config.allow_depot_drone_recover_at_station,
        )

        self._evaluate_population(population, state)

        for gen in range(self.config.generations):
            if self._timeout(started):
                break

            population.sort(key=lambda ind: ind.fitness)
            self._log_generation(gen, population)

            new_population = []
            elite_n = max(1, int(self.config.elite_ratio * self.config.population_size))
            new_population.extend(copy.deepcopy(population[:elite_n]))

            while len(new_population) < self.config.population_size:
                p1 = tournament_select(population, self.config.tournament_k)
                p2 = tournament_select(population, self.config.tournament_k)

                if random.random() < self.config.crossover_rate:
                    c1, c2 = order_crossover(p1, p2)
                else:
                    c1, c2 = copy.deepcopy(p1), copy.deepcopy(p2)

                for child in (c1, c2):
                    mutate(
                        child,
                        gene_pool=gene_pool,
                        depot_ids=depot_ids,
                        station_ids=station_ids,
                        p_seq=self.config.mutation_rate_sequence,
                        p_assign=self.config.mutation_rate_assignment,
                        p_rendezvous=self.config.mutation_rate_rendezvous,
                        allow_c_recover_station=self.config.allow_depot_drone_recover_at_station,
                    )
                    self._evaluate_individual(child, state)
                    new_population.append(child)
                    if len(new_population) >= self.config.population_size:
                        break

            population = new_population

        population.sort(key=lambda ind: ind.fitness)
        best = population[0]
        self.last_best_individual = copy.deepcopy(best)
        return best.decoded_plan

    def _evaluate_population(self, population, state):
        for ind in population:
            self._evaluate_individual(ind, state)

    def _evaluate_individual(self, ind, state):
        ind.validate()
        result = self.decoder.decode(ind, state)
        ind.fitness = result.objective
        ind.decoded_plan = result.plan
        ind.penalties = result.penalties

    def _timeout(self, started):
        if self.config.max_runtime_seconds is None:
            return False
        return time.time() - started >= self.config.max_runtime_seconds

    def _make_greedy_seed_plan(self, state):
        # 这里可以调用完整 greedy 生成 seed，但仅用于初始化 Individual。
        # 不能在 GA decoder 中调用 greedy。
        return self.greedy_helper.dispatch_replan_current_state(
            replan_orders=state.orders,
            current_time=state.current_time,
            bbox=state.bbox,
            scene_id=getattr(state, "scene_id", None),
        )

    def _log_generation(self, gen, population):
        if not self.config.verbose or gen % 10 != 0:
            return
        best = population[0]
        avg = sum(ind.fitness for ind in population) / max(1, len(population))
        feasible_count = sum(1 for ind in population if not ind.penalties)
        logger.info(
            "[GA-MMCE] gen=%s best=%.2f avg=%.2f feasible=%s penalties=%s",
            gen,
            best.fitness,
            avg,
            feasible_count,
            best.penalties,
        )

    def _empty_plan(self):
        from greedy_mmce import DispatchPlan
        return DispatchPlan(
            allocations=[],
            cost_total=0.0,
            summary={
                "total_orders": 0,
                "feasible": 0,
                "modes": {},
                "dispatch_type": "ga_mmce",
                "cost_breakdown": {"dist": 0.0, "energy": 0.0, "penalty": 0.0},
            },
        )
```

## 12.4 给 Codex 的提示词

```text
请新增/修改 backend/solver/ga_mmce/solver.py。

实现 GAMMCESolver。

要求：
1. 构造函数接收 entity_mgr 和 GAConfig。
2. 内部可以创建 GreedyMMCE 作为 greedy_helper，但 greedy_helper 只用于：
   - 物理工具函数；
   - greedy seed 初始化。
3. solve(state, warm_start=None) 必须使用三层种群。
4. solve 不能在解码阶段调用 greedy dispatch。
5. mutation 必须同时支持 sequence、assignment、rendezvous。
6. 保存 last_best_individual，用于动态重调度 warm start。
7. 返回值必须兼容前端使用的 DispatchPlan。
```

---

# 13. Step 10：接入 decision_engine.py

## 13.1 目标

让系统可以通过配置选择：

```yaml
solver: greedy_mmce
```

或：

```yaml
solver: ga_mmce
```

## 13.2 修改文件

```text
backend/solver/decision_engine.py
backend/config/loader.py
backend/config/*.yaml
```

## 13.3 给 Codex 的提示词

```text
请修改 decision_engine.py 和配置加载逻辑，使系统支持 solver = "ga_mmce"。

要求：
1. 原有 greedy_mmce 行为保持不变。
2. 当配置 solver.name 或 solver.type 为 "ga_mmce" 时：
   - 创建 GAMMCESolver(entity_mgr, GAConfig)
   - 调用 solver.solve(state)
3. GA solver 的输出必须沿用现有前端可识别的 DispatchPlan 结构。
4. 不要修改前端。
5. 如果当前配置文件没有 solver 字段，请添加默认值，默认仍使用 greedy_mmce。
```

---

# 14. Step 11：实现动态重调度模块

## 14.1 目标

动态订单到达时，调用：

```python
reschedule_on_event(...)
```

完成：

```text
1. 读取当前时刻；
2. 冻结已完成订单；
3. 冻结正在执行订单；
4. 合并剩余订单 + 新订单；
5. 从当前实体状态重新 GA；
6. 使用 last_best_individual 构造 warm start；
7. 返回新计划。
```

## 14.2 新增文件

```text
backend/solver/ga_mmce/dynamic_rescheduler.py
```

## 14.3 warm start 必须保留 rendezvous

```python
def build_warm_start(
    previous_best,
    completed_ids,
    locked_ids,
    new_order_ids,
    gene_pool,
    depot_ids,
    station_ids,
    allow_c_recover_station=True,
):
    from .chromosome import Individual
    from .operators import make_random_rendezvous_for_gene

    excluded = set(completed_ids) | set(locked_ids)

    seq = []
    assignment = []
    rendezvous = []

    if previous_best is not None:
        for oid, gene, rv in zip(
            previous_best.sequence,
            previous_best.assignment,
            previous_best.rendezvous,
        ):
            if oid not in excluded:
                seq.append(oid)
                assignment.append(gene)
                rendezvous.append(rv)

    for oid in new_order_ids:
        c_genes = [g for g in gene_pool if g.startswith("C_")]
        b_genes = [g for g in gene_pool if g.startswith("B_")]
        gene = c_genes[0] if c_genes else (b_genes[0] if b_genes else "A")
        seq.append(oid)
        assignment.append(gene)
        rendezvous.append(
            make_random_rendezvous_for_gene(
                gene,
                depot_ids,
                station_ids,
                allow_c_recover_station,
            )
        )

    return Individual(seq, assignment, rendezvous)
```

## 14.4 给 Codex 的提示词

```text
请新增/修改 backend/solver/ga_mmce/dynamic_rescheduler.py。

要求：
1. reschedule_on_event(state, new_orders, event_time) 是主入口。
2. 不要中断正在执行的物理动作。
3. completed 订单不再进入 GA。
4. locked 订单不再进入 GA，但对应实体在 locked action 完成前不可用。
5. 新订单加入当前订单池。
6. warm start 必须从 previous_best 的三层染色体生成。
7. 新订单插入时，必须同步生成 assignment 和 rendezvous。
8. 最后调用 GAMMCESolver.solve(snapshot, warm_start=[...])。
9. 如果项目已有事件系统或状态推进函数，请复用，不要新写一套仿真器。
```

---

# 15. Step 12：添加单元测试

## 15.1 新增测试文件

```text
tests/solver/test_ga_chromosome.py
tests/solver/test_ga_operators.py
tests/solver/test_ga_population.py
tests/solver/test_ga_physical_evaluator.py
tests/solver/test_ga_decoder_smoke.py
tests/solver/test_ga_dynamic_rescheduler.py
```

## 15.2 测试重点

### 15.2.1 染色体合法性

```text
- sequence / assignment / rendezvous 长度不一致应报错；
- sequence 有重复订单应报错；
- A 的 rendezvous 不是 None 应报错；
- B 缺少 launch 或 recover 应报错；
- C 的 launch 不是 depot 应报错。
```

### 15.2.2 OX 交叉

```text
- 子代订单不重复；
- 子代订单集合等于父代订单集合；
- assignment 与 rendezvous 均按订单 ID 对齐；
- 子代 validate 通过。
```

### 15.2.3 变异

```text
- 不丢订单；
- 不增加订单；
- gene 必须来自 gene_pool；
- rendezvous 仍满足模式约束。
```

### 15.2.4 PhysicalEvaluator

```text
- evaluate_fixed_mode_b 使用指定 launch/recover；
- 不允许自动替换 recovery；
- 电量不足时返回 energy_not_enough；
- 非法站点时返回 illegal_launch_node / illegal_recover_node。
```

### 15.2.5 Decoder smoke test

```text
- 3 个订单、1 辆卡车、1 架无人机、1 个换电站；
- 给定固定 rendezvous；
- GA 能返回 DispatchPlan；
- 每个订单最多服务一次；
- truck route 顺序与 sequence/rendezvous 一致；
- 不调用 greedy.dispatch。
```

### 15.2.6 动态重调度

```text
- 已完成订单不会再次进入 active orders；
- locked 订单不会被重新分配；
- 新订单会进入 active orders；
- warm start 保留旧订单的 rendezvous；
- 新订单生成合法 rendezvous。
```

## 15.3 给 Codex 的提示词

```text
请为 ga_mmce 添加单元测试。

优先测试：
1. Individual.validate 三层约束。
2. order_crossover 是否按订单 ID 对齐 assignment 和 rendezvous。
3. mutate 是否保持三层染色体合法。
4. initialize_population 是否生成合法三层个体。
5. PhysicalEvaluator 是否固定使用 GA 指定 launch/recover。
6. GADecoder 是否不调用 greedy.dispatch。
7. DynamicGARescheduler 是否正确处理 completed/locked/new orders 和 warm start。

如果真实实体构造复杂，可以使用 mock state 或项目已有 fixture。
```

---

# 16. Step 13：添加运行日志和调试信息

## 16.1 推荐日志

每 10 代输出：

```text
generation
best_fitness
avg_fitness
best_penalties
feasible_count
best_sequence
best_assignment
best_rendezvous
```

## 16.2 给 Codex 的提示词

```text
请给 GAMMCESolver 添加调试日志。

要求：
1. 使用项目现有 logging 方式。
2. 每 10 代输出：
   - generation
   - best fitness
   - average fitness
   - feasible individual count
   - best penalties
   - best sequence
   - best assignment
   - best rendezvous
3. 日志开关由 GAConfig.verbose 控制。
4. 不要使用 print。
```

---

# 17. 第一次集成验收

完成上述步骤后，运行：

```bash
pytest tests/solver/test_ga_chromosome.py
pytest tests/solver/test_ga_operators.py
pytest tests/solver/test_ga_population.py
pytest tests/solver/test_ga_physical_evaluator.py
pytest tests/solver/test_ga_decoder_smoke.py
pytest tests/solver/test_ga_dynamic_rescheduler.py
```

然后跑一个小规模场景：

```text
订单数：5
无人机数：1 或 2
换电站：2
动态订单：先不启用
solver：ga_mmce
```

验收标准：

```text
1. 后端不报错；
2. 前端能显示路线；
3. 每个订单只出现一次；
4. GA 的 truck route 顺序与 sequence/rendezvous 解码结果一致；
5. B 模式的 launch/recover 来自 rendezvous，而不是 greedy 自动选择；
6. GA 结果不一定优于 greedy，但必须可解释；
7. 若不可行，penalty 能说明原因。
```

---

# 18. 推荐给 Codex 的总代码任务说明

可以把下面这整段直接发给 Codex：

```text
我要在现有项目中新增一个遗传算法求解器，不要重写环境、实体、前端。

重要修改：
GA 必须使用三层染色体：
1. sequence：订单顺序
2. assignment：模式/载具分配，A / B_<drone_id> / C_<drone_id>
3. rendezvous：协同节点选择
   - A: None
   - B: {"launch": <depot_or_station_id>, "recover": <depot_or_station_id>}
   - C: {"launch": <depot_id>, "recover": <depot_or_station_id>}

背景：
- greedy_mmce.py 是完整贪心调度器，会自己排序、选模式、选起飞站点、选回收点、构建最近邻卡车路径。
- 因此 GA Decoder 不能调用完整 greedy dispatch，否则 GA 会被架空。
- GA 只能复用 greedy_mmce.py 中的底层物理工具函数。

请按以下步骤实现：

1. 新增 backend/solver/ga_mmce/ 包。
2. 新增 config.py，实现 GAConfig，包含 mutation_rate_rendezvous。
3. 新增 chromosome.py，实现三层 Individual。
4. 新增 operators.py，实现 OX 交叉、assignment mutation、rendezvous mutation。
5. 新增 population.py，实现三层种群初始化。
6. 新增 adapters.py，提取 active orders、drone ids、truck ids、depot ids、station ids，并把 greedy plan 转成三层 Individual。
7. 新增 physical_evaluator.py，实现固定决策评估：
   - evaluate_fixed_mode_a
   - evaluate_fixed_mode_b
   - evaluate_fixed_mode_c
   这些函数必须使用 GA 指定的 launch/recover，不允许自动搜索最近站点。
8. 新增 decoder.py，实现 GADecoder：
   - 按 sequence 顺序处理订单；
   - 按 assignment 固定模式；
   - 按 rendezvous 固定起飞/回收节点；
   - 使用 build_incremental_route_from_stops 按给定 ordered_stops 构建卡车路线；
   - 不允许调用 greedy.dispatch / _dispatch_impl / _allocate_order / _build_truck_route。
9. 新增 fitness.py，统一计算适应度。
10. 新增 solver.py，实现 GAMMCESolver.solve(state, warm_start=None)。
11. 修改 decision_engine.py，使配置 solver = "ga_mmce" 时调用 GAMMCESolver。
12. 新增 dynamic_rescheduler.py，实现新订单到达时的事件触发式重调度，并保留 rendezvous warm start。
13. 添加 tests/solver 下的单元测试。
14. 每一步完成后运行测试，确保 greedy 原功能不被破坏。

禁止 GA Decoder 调用：
- GreedyMMCE.dispatch
- GreedyMMCE.dispatch_incremental
- GreedyMMCE.dispatch_replan_current_state
- GreedyMMCE._dispatch_impl
- GreedyMMCE._allocate_order
- GreedyMMCE._try_mode_b_with_waiting
- GreedyMMCE._try_mode_b
- GreedyMMCE._try_mode_c
- GreedyMMCE._try_mode_a
- GreedyMMCE._build_truck_route
- GreedyMMCE._check_energy_feasible

GA 可以复用：
- _road_dist
- _dist
- _flight_energy
- _uav_energy_wh
- _truck_energy_wh
- _recalculate_plan_route_costs
- build_incremental_route_from_stops

重要约束：
- 不要修改前端。
- 不要重写实体状态机。
- 不要复制一套能耗计算。
- GA 解码必须在 state 副本上执行，不能污染真实环境。
- 每个订单必须且只能服务一次。
- 无人机携货阶段不允许中途换电。
- 模式 B 的 launch/recover 只能是仓库或充换电站。
- 模式 B 的回收点不能是客户点。
- 动态重调度不能中断正在执行的飞行或服务动作。
```

---

# 19. MVP 实现建议

原 MVP 里“decoder 暂时调用 greedy 整体 solve”不再推荐，因为这会掩盖 GA 的真实作用。

推荐新 MVP：

```text
MVP 1：
    chromosome.py
    operators.py
    population.py
    三层染色体单元测试

MVP 2：
    physical_evaluator.py
    先实现 evaluate_fixed_mode_a / B / C 的基本可行性判断

MVP 3：
    decoder.py
    使用固定 sequence + assignment + rendezvous 生成 DispatchPlan
    truck route 使用 build_incremental_route_from_stops

MVP 4：
    solver.py
    完成 GA 迭代

MVP 5：
    接入 decision_engine.py

MVP 6：
    dynamic_rescheduler.py + warm start
```

临时允许：

```text
greedy 作为 seed 生成器。
```

临时不允许：

```text
greedy 作为 GA decoder。
```

---

# 20. 最终调用链

静态调度调用链：

```text
decision_engine.py
    ↓
GAMMCESolver.solve(state)
    ↓
initialize_population(sequence + assignment + rendezvous)
    ↓
for each generation:
    ↓
GADecoder.decode(individual, state_copy)
    ↓
PhysicalEvaluator.evaluate_fixed_mode_A/B/C(...)
    ↓
按 GA 指定的 ordered_stops 构建 truck route
    ↓
build_incremental_route_from_stops(...)
    ↓
fitness / objective
    ↓
selection / crossover / mutation
    ↓
best.decoded_plan
    ↓
前端展示
```

动态订单调用链：

```text
dynamic order event
    ↓
DynamicGARescheduler.reschedule_on_event()
    ↓
snapshot current state
    ↓
remove completed orders
    ↓
freeze locked orders
    ↓
insert new orders
    ↓
build warm start with sequence + assignment + rendezvous
    ↓
GAMMCESolver.solve(snapshot, warm_start)
    ↓
new plan
```

---

# 21. 推荐 commit 顺序

```text
commit 1: add GAConfig with rendezvous mutation config
commit 2: add three-layer Individual
commit 3: add GA operators with rendezvous crossover/mutation
commit 4: add population init for three-layer chromosome
commit 5: add adapters for depot/station/truck extraction
commit 6: add physical_evaluator fixed-mode evaluation
commit 7: add GADecoder using fixed decisions
commit 8: add GAMMCESolver
commit 9: integrate decision_engine
commit 10: add physical evaluator and decoder tests
commit 11: add dynamic rescheduler with rendezvous warm start
commit 12: add logging and integration tests
```

最重要的原则：

```text
GA 决定 sequence / assignment / rendezvous。
GreedyMMCE 只作为底层物理工具和 seed 生成器。
不要让 greedy 决定 GA 个体的路径结构。
```
