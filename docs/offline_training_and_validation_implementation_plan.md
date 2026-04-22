# RH-ALNS + CMRAPPO 离线训练与离线验证阶段实施计划

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

---

## 1. 阶段目标

本阶段只回答 4 个问题：

1. 训练环境能否稳定构建并重复运行
2. RH-ALNS + CMRAPPO 能否在 `backend/test_data/default_scene` 基线场景上跑出可解释结果
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
  + Lambda_hard * N_hard_fail
```

含义如下：

- `T_complete`：订单总完成时间或总完成时长汇总
- `T_wait`：卡车与无人机互相等待时间总和，其中无人机等待卡车必须纳入小惩罚
- `T_queue`：无人机在充换电站排队总时间
- `T_fallback`：错过卡车后飞往下一个站点或仓库的总时间
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

## 4.1 Phase 1：固化离线阶段的数据与参数契约

目标：

- 先定义训练到底吃什么、输出什么

需要完成：

1. 固定离线训练阶段主输入：
   - `scene_config.json`
   - `entities.json`
   - `orders.json`
   - `osm_network.xml/geojson`

2. 定义两类订单源：
   - 固定 benchmark：`static_orders + dynamic_orders`
   - 随机扩展：基于 `arrival_rate` 的泊松流

3. 定义训练专属配置文件：
   - `backend/config/rh_alns_cmrappo.yaml`

4. 定义模型元数据文件格式：
   - `backend/weights/rh_alns_cmrappo/<version>/meta.json`

5. 定义环境语义契约：
   - 模式 C 合法回收条件
   - 错过卡车后的 fallback 规则
   - 硬失败判定条件
   - FIFO / 工位容量 / 终端清场逻辑

参数边界要求：

- 共享运行时参数：
  - 能耗模型
  - 配送时长
  - 操作时长

- 新算法专属参数：
  - 目标权重
  - 奖励项
  - 惩罚项
  - 候选集参数
  - ALNS 参数
  - 模型结构参数

产物：

- `rh_alns_cmrappo.yaml` 初版
- `meta.json` schema 初版
- 离线阶段参数清单
- 一份环境语义契约说明

验收标准：

- 任何人只看配置文件就能知道训练输入和训练参数来源
- 不再混用旧共享配置与新算法专属权重/惩罚参数
- `env_adapter` 的后续实现不会再对模式 C / miss / queue 语义各自理解

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
2. 动作代表什么
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
   - 返程回收点选择
   - 卡车到站
   - 无人机到站
   - 错过卡车
   - FIFO 入队 / 出队
   - 半空中能源硬失败

3. 奖励与惩罚来源
   - `T_complete`
   - `T_wait`
   - `T_queue`
   - `T_fallback`
   - `N_hard_fail`

动作建议：

- 基于候选订单 + 候选回收点 + 候选模式构造离散动作索引
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

## 4.4 Phase 4：实现候选动作与上层规划骨架

目标：

- 先把训练真正依赖的动作空间稳定下来

建议新增文件：

- `backend/training/candidate_builder.py`
- `backend/training/planner_bridge.py`

职责拆分：

1. `candidate_builder.py`
   - 构建候选订单
   - 构建候选回收点
   - 构建动作 mask

2. `planner_bridge.py`
   - 承接 RH-ALNS 上层骨架
   - 管理重规划周期、候选裁剪、ALNS 调用入口

这一阶段必须先固定以下参数：

- `support_radius_km`
- `max_candidate_orders`
- `max_candidate_recovery_per_order`
- `max_candidate_actions`
- `station_wait_threshold_sec`
- `upper_replan_interval_sec`
- `upper_replan_new_order_trigger`
- `upper_horizon_sec`
- `alns.iters`
- `destroy_ratio`

原因：

- 这些参数直接定义动作空间
- 动作空间变了，训练权重的语义也会变

同时必须把以下语义写进候选动作生成器：

1. 模式 C 候选回收点只能来自卡车未来合法交接节点或保底合法节点
2. 若 `ETA_uav + delta_safe > ETA_truck`，该回收动作必须直接裁掉
3. 若电量不足以到达候选回收点并保留安全余量，该动作必须直接裁掉
4. 若模式 C 原候选失效，fallback 只能指向最近可达充换电站或仓库

产物：

- 候选动作生成器
- action mask 生成器
- 上层规划骨架入口

验收标准：

- 在 `default_scene` 上每一步都能稳定生成候选集
- `max_candidate_actions` 不溢出
- mask 与候选动作一一对应

## 4.5 Phase 5：实现 PPO 训练主循环

目标：

- 让环境和模型真正连起来，产出第一版权重

建议新增文件：

- `backend/training/train_cmrappo.py`
- `backend/training/model.py`
- `backend/training/rollout_buffer.py`

最小训练闭环需要：

- 模型前向
- action mask 采样
- rollout 收集
- advantage 计算
- PPO 更新
- checkpoint 保存

训练时必须保证：

- reward 与第 2.1.4 节主目标逐项对应
- 软失败通过 `T_fallback` 进入小惩罚项
- 硬失败通过 `N_hard_fail` 进入大惩罚项
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
- reward 提升时，主目标分解项也能同步解释，而不是只表现为黑盒数值变化

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
- 主目标值
- 卡车总里程
- 无人机总里程
- 回收站平均等待
- 模式分布
- 每单平均响应时延
- 无人机等待卡车时间
- 站点排队总时间
- miss 次数
- fallback 总时间
- hard failure 次数

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
- 上层规划参数
- 奖励/惩罚参数
- 共享运行时参数快照
- 训练所用场景 ID
- 训练所用 benchmark 版本
- 后续在线锁参数策略骨架
- 当前版本冻结的环境语义契约摘要

说明：

- 虽然当前阶段还不接前后端
- 但 `meta.json` 必须在本阶段就做，因为它是下一阶段线上锁参数的基础

---

## 5. 当前最合适的实际开工顺序

建议按以下小顺序开工：

1. 先做 `backend/config/rh_alns_cmrappo.yaml`
2. 再冻结第 2.1 节的环境语义契约
3. 再做 `backend/training/scene_loader.py`
4. 再做 `backend/training/env_adapter.py`
5. 先跑固定 benchmark，不开泊松流
6. 再做 `backend/training/train_cmrappo.py`
7. 训练出第一版 `policy.pt + meta.json`
8. 做 `backend/training/validate_benchmark.py`
9. 再做 `backend/training/validate_stochastic.py`
10. 最后做 SUMO 导出

原因：

- 先把固定 benchmark 跑通，最容易暴露环境定义问题
- 随机泊松流和 SUMO 都应该放在模型能工作之后
- 否则会同时面对环境 bug、训练 bug、可视化 bug 三类问题

---

## 6. 本阶段完成判断标准

离线训练与离线验证阶段完成，至少要满足：

1. 能稳定产出 `policy.pt + meta.json`
2. 能在 `default_scene` 上完成固定 benchmark 回放
3. 能输出 benchmark 指标报告
4. 能做至少一组随机泊松扩展验证
5. 能导出 SUMO 可视化文件并复核结果
6. 已明确哪些参数属于后续在线必须锁定
7. 模式 C、miss、fallback、FIFO 与 hard failure 语义已在环境和验证中闭环

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
