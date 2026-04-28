# Phase7 Run01 训练复盘与 Run02 参数调整记录

**基准 run**：`phase7_20260428_run01`（1.5M steps，367 updates）  
**调整目标**：修复熵崩溃、WAIT 动作主导、Critic 不稳定三个核心问题  
**修改文件**：`backend/config/rh_alns_cmrappo.yaml`

---

## 一、Run01 核心问题诊断

### 1. 熵崩溃（Entropy Collapse）

| 指标 | 数值 |
|------|------|
| 熵崩溃时间点 | update=23，global_step≈94k（仅 6% 训练进度） |
| 崩溃后熵范围 | 0.0000 ~ 0.17，剧烈震荡 |
| 训练末期熵均值（后 20 updates） | 0.072 |

策略在训练极早期就坍缩为确定性策略，之后在"崩溃→被扰动→再崩溃"的循环中打转，无法稳定探索。根本原因：`entropy_coef=0.01` 过低，熵正则化强度不足以对抗策略的过早收敛。

### 2. WAIT 动作主导（Wait Action Dominance）

| 指标 | 数值 |
|------|------|
| benchmark WAIT 占比 | 90%（36/40 次决策） |
| benchmark 末尾 pending 订单 | 4 个（从未被派出） |
| benchmark 总 reward | -1514 |
| benchmark timeout 订单 | 2，hard_overdue 订单 2 |

策略学会了"不派单"。根本原因是奖励结构的风险/收益严重失衡：

```
成功送达奖励：        +60
超时惩罚（per_dt）：  -0.20 × 600s = -120
hard_overdue 额外：   -0.20 × 600s = -120
派单失败最坏情况：    -240（是送达奖励的 4 倍）
```

同时 `lambda_wait=0.10` 的等待惩罚太小，不足以逼迫策略承担派单风险。

### 3. Critic 不稳定（Value Loss Instability）

| 指标 | 数值 |
|------|------|
| value_loss 峰值 | update=321 时达到 24391 |
| value_loss >100 的 updates 数 | 98 次（占总 367 次的 27%） |

代码中未实现 value function clipping（标准 PPO 的 `vf_clip_coef`），Critic 从随机初始化开始面对量级为 100~1200 的 return，反复出现巨大 loss spike。`value_loss_coef=0.5` 进一步放大了 Critic 不稳定对 policy 梯度的污染。

### 4. KL 超标导致 early stopping 过于频繁

`target_kl=0.03` 过紧，实际 KL 经常达到 0.05~0.22，导致每个 rollout 实际只完成 1~2 个 epoch 的更新，样本利用率低。

---

## 二、Run02 参数调整内容

### 2.1 reward 调整

| 参数 | Run01 | Run02 | 调整原因 |
|------|-------|-------|---------|
| `lambda_wait` | 0.10 | **0.25** | 加大等待惩罚，逼迫策略主动派单 |
| `wait_idle_penalty_coef` | 0.10 | **0.25** | 同上，与 lambda_wait 联动 |
| `R_delivery_bonus` | 60 | **100** | 提高送达激励，改善风险/收益比（送达奖励从惩罚的 1/4 提升到接近 1/2） |
| `lambda_overdue` | 0.20 | **0.15** | 适度降低超时惩罚，减少策略对派单风险的过度厌恶 |

调整后的风险/收益对比：

```
成功送达奖励：        +100
超时惩罚（per_dt）：  -0.15 × 600s = -90
hard_overdue 额外：   -0.15 × 600s = -90
派单失败最坏情况：    -180（是送达奖励的 1.8 倍，Run01 为 4 倍）
```

### 2.2 training 调整

| 参数 | Run01 | Run02 | 调整原因 |
|------|-------|-------|---------|
| `entropy_coef` | 0.01 | **0.03** | 3x 增强熵正则化，防止策略过早坍缩为确定性 |
| `value_loss_coef` | 0.50 | **0.25** | 降低 Critic loss 对 policy 梯度的污染权重，缓解 value loss spike 的影响 |
| `target_kl` | 0.03 | **0.05** | 放宽 KL 阈值，允许每个 rollout 完成更充分的 epoch 更新，提高样本利用率 |

---

## 三、未在 Run02 实施的后续优化项

以下问题已识别，但需要代码改动，留待后续 run 处理：

1. **Value function clipping**：在 `train_cmrappo.py` 的 `_ppo_update` 中加入 `vf_clip_coef`，从根本上解决 Critic 不稳定问题。

2. **Reservation timeout 过高**：stochastic 各档几乎每次派单都伴随 reservation timeout（low: 13次/5ep，high: 38次/5ep），需要检查 `alpha=1.5, beta=1.2` 的预约窗口估算是否过于保守。

3. **Curriculum 开启**：当前 `curriculum.enabled: false`，待 Run02 验证基础行为正常后，Run03 可开启 `station_queue_noise` 和 `truck_delay_noise` 提升泛化能力。

---

## 四、Run02 预期改善目标

| 指标 | Run01 实际 | Run02 目标 |
|------|-----------|-----------|
| benchmark WAIT 占比 | 90% | <50% |
| benchmark pending orders | 4 | 0~1 |
| benchmark reward | -1514 | >-200 |
| 训练熵（稳定期） | 0~0.17 震荡 | 0.05~0.15 稳定 |
| value loss spike（>1000） | 频繁 | 偶发或消失 |
