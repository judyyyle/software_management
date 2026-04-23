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

其中需要明确区分两类订单输入：

1. 固定 benchmark 订单
   - `orders.json.static_orders`
   - `orders.json.dynamic_orders`

2. 随机训练扩展订单
   - 由 `arrival_rate > 0` 的泊松流额外生成

实施原则：

- 第一阶段先跑固定 benchmark
- 第二阶段再叠加随机泊松流

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

当前阶段统一采用以下主目标：

```text
J =
  T_complete
  + lambda_wait * T_wait
  + lambda_queue * T_queue
  + lambda_miss * T_fallback
   + lambda_res_timeout * T_res_timeout
  + Lambda_hard * N_hard_fail
```

含义如下：

- `T_complete`：所有订单从生成时刻到完成时刻的时长总和，定义为
   `T_complete = sum_i (t_complete_i - t_generate_i)`
   示例：3 个订单分别耗时 10 分钟、20 分钟、40 分钟，则
   `T_complete = 70 分钟`。
   该目标强调“整体尽早完成”，避免大量订单被长期拖延
- `T_wait`：卡车与无人机互相等待时间总和，其中无人机等待卡车必须纳入小惩罚
- `T_queue`：无人机在充换电站排队总时间
- `T_fallback`：错过卡车后飞往下一个站点或仓库的总时间
- `T_res_timeout`：reservation timeout 造成的总局部损失
- `N_hard_fail`：半空停电或无合法安全节点可达的事件数

要求：

- 训练 reward 必须来自这套主目标及其分解项
- benchmark 与 stochastic 验证也必须回到这套指标，不允许只看 PPO reward

### 2.1.5 当前阶段建议采用事件驱动环境

由于模式 C 的赶上 / 错过、FIFO 队列与站点容量都对时间精度敏感，本阶段的离线环境建议采用事件驱动或至少准事件驱动推进，而不是大时间步粗近似。

否则会直接导致：

- 模式 C 候选动作判定失真
- `action_mask` 不稳定
- miss / catch 统计不可靠
- 奖励噪声异常增大

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
4. 给未来一段时间内的回收节点分配粗粒度负载预算（`node_load_budget`）
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
4. 训练阶段固定使用 `centralized_train_only critic`
5. 推理阶段默认使用 `greedy` 动作选择

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
  node_load_budget: {node_id -> int}
  route_drift_ref: {node_id -> (eta_ref, route_index_ref)}
}
```

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

2. 定义两类订单源：
   - 固定 benchmark：`static_orders + dynamic_orders`
   - 随机扩展：基于 `arrival_rate` 的泊松流（第二阶段启用）

3. 定义训练专属配置文件：
   - `backend/config/rh_alns_cmrappo.yaml`
   - 配置文件必须按以下分层组织：`scene` / `planner` / `candidate` / `action_space` / `reservation` / `policy` / `reward` / `training` / `curriculum`

4. 定义模型元数据文件格式：
   - `backend/weights/rh_alns_cmrappo/<version>/meta.json`

5. 冻结环境语义契约（见 4.1.3 节）

### 4.1.3 必须先冻结的环境语义契约

离线训练与离线验证不能只依赖参数文件，还必须先冻结以下 5 件事：

**（1）模式 C 的合法回收动作**

模式 C 在离线环境中的正式语义：

- 无人机送达后，不允许飞向道路中途的运动中卡车
- "返回卡车"只能解释成返回卡车未来将经过的合法交接节点
- 合法交接节点仅限：`station` 或 `depot`

候选回收动作仅在同时满足以下条件时才能保留在 `action_mask` 中：

1. 该节点在卡车未来路径上，且仍未经过（`ETA_truck(r_j) > t_deliver`）
2. 无人机预计到达时刻早于卡车到达该节点（`ETA_uav(deliver -> r_j) + delta_safe <= ETA_truck(r_j)`）
3. 无人机剩余电量足以飞到该节点并保留安全余量（`E_need + E_safe <= E_rem`）
4. 节点容量与站点状态未被硬性否决

**（2）模式 C 返程的保底规则**

订单送达后，返程目标按以下顺序选择：

1. 卡车未来合法交接节点（mode C）
2. 仓库（depot）
3. 最近可达充换电站
4. 若上述均不可达，则进入硬失败

**（3）Reservation Timeout 机制（CCT 映射）**

本阶段必须实现 reservation timeout，这是 CCT 机制在本项目的正式映射：

- reservation 是"软预留、非排他、容量感知"的局部承诺，不是物理工位锁
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

1. `parking_slots` 决定并行服务上限
2. 超出容量必须进入 FIFO 队列
3. `swap_time` / `charge_time` 结束后释放工位并唤醒队首
4. 排队时间按"入队时刻到开始服务时刻"精确累计
5. episode 结束时必须清点：已完成订单、超时订单、仍在返程/排队/等待卡车/已硬失败的无人机

### 4.1.4 主目标函数（本阶段统一采用）

```text
J =
  T_complete
  + lambda_wait * T_wait
  + lambda_queue * T_queue
  + lambda_miss * T_fallback
  + lambda_res_timeout * T_res_timeout
  + Lambda_hard * N_hard_fail
```

含义：

- `T_complete`：所有订单从生成时刻到完成时刻的时长总和，定义为
   `T_complete = sum_i (t_complete_i - t_generate_i)`
   示例：3 个订单分别耗时 10 分钟、20 分钟、40 分钟，则
   `T_complete = 70 分钟`。
   该目标强调“整体尽早完成”，避免大量订单被长期拖延
- `T_wait`：卡车与无人机互相等待时间总和
- `T_queue`：无人机在充换电站排队总时间
- `T_fallback`：错过卡车后飞往下一个站点或仓库的总时间
- `T_res_timeout`：reservation timeout 造成的总局部损失
- `N_hard_fail`：硬失败次数

要求：

- 训练 reward 必须来自这套主目标及其分解项
- benchmark 与 stochastic 验证也必须回到这套指标，不允许只看 PPO reward
- `Lambda_hard` 首轮建议值为 `1200`（秒级等价损失），必须显著大于一次最坏可恢复扰动

### 4.1.5 参数分层要求

参数必须按以下分层组织，不允许混用：

- **共享运行时参数**（继续放在 `backend/config/drone_params.yaml`）：
  - 能耗模型、配送时长、操作时长、速度等物理参数

- **算法专属参数**（统一放在 `backend/config/rh_alns_cmrappo.yaml`）：
  - `scene`：地图尺度派生规则
  - `planner`：RH-ALNS 低频触发参数（`coarse_replan_interval_sec=420` 等）
  - `candidate`：候选集参数（`max_candidate_orders=32`、`max_candidate_recovery_per_order=4`、`max_candidate_actions=128`、`station_wait_threshold_sec=240` 等）
  - `action_space`：动作空间类型（`factorized`）、`enable_wait_action=true`、`include_mode_a_in_policy=false`
  - `reservation`：timeout 参数（`alpha=1.5`、`beta=1.2`、`gamma=0.3` 等）
  - `policy`：模型结构（`encoder_type=attn_lstm_lite`、`d_model=128`、`nhead=8`、`lstm_hidden=128`、`hist_len=6`、`critic_mode=centralized_train_only`、`inference_mode=greedy` 等）
  - `reward`：惩罚权重（`lambda_wait=0.10`、`lambda_queue=0.10`、`lambda_miss=0.15`、`lambda_res_timeout=0.10`、`hard_failure_penalty_sec=1200` 等）
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

## 4.2 Phase 2：实现离线场景装载器

目标：

- 把 `default_scene` 变成训练和验证都能直接读取的标准内部对象

建议新增文件：

- `backend/training/scene_loader.py`

核心职责：

1. 读取 `scene_config.json`
2. 读取 `entities.json`
3. 读取 `orders.json`
4. 读取 `osm_network.xml/geojson`
5. 输出统一的训练场景对象

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

## 4.3 Phase 3：实现训练环境适配器

目标：

- 把当前场景和调度问题封装成可训练环境

建议新增文件：

- `backend/training/env_adapter.py`

最小职责：

- `reset()`
- `step(action)`
- `build_observation()`
- `build_action_mask()`
- `compute_reward()`
- `is_done()`

第一版环境建议：

- 场景固定为 `default_scene`
- 订单源先只支持：
  - `static_orders`
  - `dynamic_orders`
- 暂不启用随机泊松扩展
- 每个 `step` 对应一次调度决策窗口
- 不要求一开始就是全精度连续物理仿真，但必须优先保证事件状态转移正确
- 环境推进建议采用事件驱动或准事件驱动，而不是粗时间步近似

需要先定清楚：

1. 观测包含什么
2. 动作代表什么（factorized：order → mode → recover_node）
3. 奖励在什么时刻结算
4. 终止条件是什么

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

2. 关键事件
   - 订单送达
   - 返程回收点选择（factorized action）
   - reservation 建立与 timeout 检查
   - 卡车到站
   - 无人机到站
   - 错过卡车（触发 fallback_recovery）
   - FIFO 入队 / 出队
   - 半空中能源硬失败

3. 奖励与惩罚来源（必须与主目标逐项对应）
   - `T_complete`
   - `T_wait`
   - `T_queue`
   - `T_fallback`
   - `T_res_timeout`（reservation timeout 造成的局部损失）
   - `N_hard_fail`

4. 局部承诺状态（reservation）
   - 当前预留的 `recover_node`
   - reservation 发起时刻与过期时刻
   - `reservation_count(node)` 作为拥挤信号
   - timeout 后自动进入 `fallback_recovery` 并施加软惩罚

动作建议：

- 采用 factorized action space：先选订单，再选模式（`WAIT`/`B`/`C`），再选回收节点
- `WAIT` 为必备动作，不允许被 mask 掉
- `mode A` 不进入 PPO 动作空间
- 全程配合 `action_mask`

产物：

- `env_adapter.py`
- 一个最小可运行 episode
- observation / action / reward 说明

验收标准：

- `reset -> step -> ... -> done` 能完整跑通
- 不出现非法动作、空 mask、订单状态错乱
- 错过卡车时订单不会被错误回滚
- 硬失败时无人机会被正确移出后续可用集合
- 站点排队和工位释放逻辑可复现
- reservation timeout 能正确触发 fallback 并记录 `T_res_timeout`

## 4.4 Phase 4：实现候选动作与上层规划骨架

目标：

- 先把训练真正依赖的动作空间稳定下来
- 实现固定 RH-ALNS 骨架的简化版本，输出 `CoarsePlanView`

建议新增文件：

- `backend/training/candidate_builder.py`
- `backend/training/planner_bridge.py`

职责拆分：

1. `candidate_builder.py`
   - 构建候选订单（在 `authorized_orders` 范围内）
   - 构建候选回收点（在 `recovery_pool[order]` 范围内）
   - 构建 factorized action mask（order → mode → recover_node 三阶段）
   - 实现 `max_candidate_actions` 截断逻辑（先按 `order_pre_score` 截断低优先级订单，再截断较差 recovery 候选，不允许截掉 `WAIT`）

2. `planner_bridge.py`
   - 承接 RH-ALNS 上层骨架，输出 `CoarsePlanView`
   - 管理重规划周期（`coarse_replan_interval_sec=420`）
   - 管理 `plan_version` 更新与失效规则
   - 本阶段 ALNS 可以简化实现（如基于贪心或固定规则），但接口必须满足 `CoarsePlanView` 契约

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
4. 若 `predicted_queue_time(node) > station_wait_threshold_sec`，该节点必须从候选集中剔除
5. 若模式 C 原候选失效，fallback 只能指向最近可达充换电站或仓库
6. `WAIT` 必须始终存在于模式阶段动作域中，不允许被 mask 掉

产物：

- 候选动作生成器（factorized）
- action mask 生成器（三阶段）
- 上层规划骨架入口（`CoarsePlanView` 输出）

验收标准：

- 在 `default_scene` 上每一步都能稳定生成候选集
- `max_candidate_actions` 不溢出
- mask 与候选动作一一对应
- `CoarsePlanView` 输出满足接口契约，PPO 可直接消费

## 4.5 Phase 5：实现 PPO 训练主循环

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

1. `uav_self_token`：当前决策 UAV 的单个 token（位置、剩余电量、状态、剩余时间、reservation 状态、`plan_version_delta`）
2. `order_tokens`：最多 `max_candidate_orders=32` 个候选订单 token
3. `recovery_tokens`：对当前所选订单的回收节点候选（最多 `max_candidate_recovery_per_order=4` 个）
4. `infra_tokens`：truck 骨架摘要 + station/depot 摘要（queue、ETA、parking_slots、node_load_budget）
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

1. 固定 benchmark 过拟合阶段
   - 目的：确认模型、奖励、动作空间没有定义错

2. benchmark + 随机扩展阶段
   - 目的：逐步获得泛化能力

训练输出必须包含：

- `policy.pt`
- `meta.json`
- `train_metrics.jsonl` 或等价训练日志
- 训练配置快照

验收标准：

- 训练前几轮不会直接发散或崩溃
- 能稳定保存权重和元数据
- 固定 benchmark 回放指标出现提升趋势
- reward 提升时，主目标分解项（含 `T_res_timeout`）也能同步解释，而不是只表现为黑盒数值变化

## 4.6 Phase 6：固定 benchmark 离线验证

目标：

- 先做确定性验证，不先做随机场景

建议新增文件：

- `backend/training/validate_benchmark.py`

输入：

- `default_scene/entities.json`
- `default_scene/orders.json`
- `policy.pt`
- `meta.json`

验证方式：

- 固定 `static_orders + dynamic_orders`
- 关闭随机泊松流：`arrival_rate = 0`
- 多次重复跑同一组场景
- 与基线算法对比

建议至少统计这些指标：

- 完成订单数
- 准时率
- 超时数
- 主目标值（`J`）及各分解项
- 卡车总里程
- 无人机总里程
- 回收站平均等待
- 模式分布（mode B / C 采用比例）
- 每单平均响应时延
- 无人机等待卡车时间（`T_wait`）
- 站点排队总时间（`T_queue`）
- miss 次数
- fallback 总时间（`T_fallback`）
- reservation timeout 次数（`T_res_timeout`）
- hard failure 次数（`N_hard_fail`）
- greedy 推理下的吞吐与稳定性指标

这一阶段最重要的是：

- 可复现性
- 可解释性

产物：

- `benchmark_report.json`
- `benchmark_summary.md`

验收标准：

- 同一权重、同一输入，多次运行结果一致或波动极小
- 至少有一项核心指标优于当前基线，或策略行为明显更合理
- 至少能解释模式 C 回收成功率、miss 率与 hard failure 率

## 4.7 Phase 7：随机扩展离线验证

目标：

- 在固定 benchmark 通过后，再验证对泊松动态订单的泛化

建议新增文件：

- `backend/training/validate_stochastic.py`

验证方式：

- 保持 `default_scene` 的设施和地图不变
- 订单源改为：
  - `static_orders` 可保留或关闭
  - `dynamic_orders` 可保留作为背景事件
  - 再叠加 `arrival_rate > 0` 的泊松流
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

## 4.8 Phase 8：SUMO 可视化验证

目标：

- 把离线验证结果放进 `sumo-gui` 里看，而不是先接前端

建议新增文件：

- `backend/training/export_sumo_facilities.py`
- `backend/training/export_sumo_replay.py`

职责：

1. `export_sumo_facilities.py`
   - 把 depot / station 导出为 `poi.add.xml` 或类似 SUMO additional 文件

2. `export_sumo_replay.py`
   - 把离线验证产生的路线、事件、订单结果导出成 SUMO 可观察文件

数据来源优先顺序：

1. 优先直接复用 `default_scene` 中已有设施坐标
2. 如果需要测试“重新布局算法”，再增加 `gridLayoutSplit` 的 Python 同构版
3. 先不要和现有前端坐标同步逻辑混用

这一阶段主要看：

- depot / station 布局是否合理
- 路径是否与场景拓扑一致
- 调度行为是否出现明显不合理绕行或聚集
- 模式 C 的 rendezvous、miss 与 fallback 行为是否在时间轴上可解释
- 是否出现明显违反站点容量或空中失效后仍继续派单的错误

产物：

- `poi.add.xml`
- 回放导出文件
- SUMO 运行说明

验收标准：

- `sumo-gui` 能正确加载
- 设施和路径位置正确
- 至少完成 1 个 benchmark 回放可视化

## 4.9 Phase 9：模型产物固化

目标：

- 为下一阶段“在线接入”做好准备

每个训练版本建议固化为如下目录：

```text
backend/weights/rh_alns_cmrappo/<version>/
  policy.pt
  meta.json
  benchmark_report.json
  stochastic_report.json
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
- 训练所用 benchmark 版本
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
3. 再做 `backend/training/scene_loader.py`
4. 再做 `backend/training/env_adapter.py`（含 reservation timeout 状态与事件）
5. 再做 `backend/training/candidate_builder.py` 和 `planner_bridge.py`（固定 RH-ALNS 骨架，输出 `CoarsePlanView`）
6. 先跑固定 benchmark，不开泊松流
7. 再做 `backend/training/train_cmrappo.py`（Shared PPO-Lite，factorized action space，centralized_train_only critic）
8. 训练出第一版 `policy.pt + meta.json`
9. 做 `backend/training/validate_benchmark.py`（greedy inference）
10. 再做 `backend/training/validate_stochastic.py`
11. 最后做 SUMO 导出

原因：

- 先把固定 benchmark 跑通，最容易暴露环境定义问题
- RH-ALNS 固定后，PPO 的输入分布稳定，训练更容易收敛
- reservation timeout 必须在 env_adapter 阶段就实现，不能推迟
- 随机泊松流和 SUMO 都应该放在模型能工作之后
- 否则会同时面对环境 bug、训练 bug、可视化 bug 三类问题

---

## 6. 本阶段完成判断标准

离线训练与离线验证阶段完成，至少要满足：

1. 能稳定产出 `policy.pt + meta.json`
2. 能在 `default_scene` 上完成固定 benchmark 回放（greedy inference）
3. 能输出 benchmark 指标报告（含 `T_res_timeout`、reservation timeout 次数）
4. 能做至少一组随机泊松扩展验证
5. 能导出 SUMO 可视化文件并复核结果
6. 已明确哪些参数属于后续在线必须锁定
7. 模式 C、miss、fallback、reservation timeout、FIFO 与 hard failure 语义已在环境和验证中闭环
8. RH-ALNS 与 CMRAPPO 的职责边界在代码层面已明确分离（`planner_bridge.py` 与 `env_adapter.py` 各司其职）

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
