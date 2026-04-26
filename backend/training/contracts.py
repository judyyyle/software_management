#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — RH-ALNS / CMRAPPO 共享契约定义。

设计目标：
  - 将文档中的 `CoarsePlanView` 正式收敛为代码级只读契约；
  - 明确 RH-ALNS 粗规划输出与运行时真值状态之间的边界；
  - 为后续 `planner_bridge`、`candidate_builder`、`env_adapter`、baseline
    提供统一的类型依赖，避免再次回到裸字典 + 魔法字符串。

重要边界：
  - `CoarsePlanView` 不是运行时真值，也不是最终 action mask。
  - 它表达的是“低频粗规划层对 PPO 的只读边界快照”。
  - 最终的局部动作合法性仍需由 `candidate_builder` 基于当前运行时状态判定。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, FrozenSet, Mapping, TypeAlias


NodeId: TypeAlias = str
OrderId: TypeAlias = str
PlanVersion: TypeAlias = int
EtaSec: TypeAlias = float
SimTimeSec: TypeAlias = float
PriorityBand: TypeAlias = int
NodeChargeLoadBudget: TypeAlias = int


class PlannerMode(StrEnum):
    """上层粗规划允许的配送模式。"""

    A = "A"
    B = "B"
    C = "C"


class PolicyMode(StrEnum):
    """真正暴露给 PPO actor 的模式词表。"""

    WAIT = "WAIT"
    B = "B"
    C = "C"


_POLICY_TO_PLANNER_MODE: Mapping[PolicyMode, PlannerMode] = {
    PolicyMode.B: PlannerMode.B,
    PolicyMode.C: PlannerMode.C,
}


@dataclass(frozen=True)
class RouteDriftRef:
    """
    路线漂移参考基线。

    用于回答：
      - 当前 coarse plan 认为卡车应在何时到达该节点？
      - 该节点在骨架路线中的参考顺序位置是什么？

    后续 route drift 检查、reservation 失效和 replan 触发都以此为参考系。
    """

    eta_ref: EtaSec
    route_index_ref: int

    def __post_init__(self) -> None:
        if self.eta_ref < 0:
            raise ValueError(f"eta_ref 不能为负数: {self.eta_ref}")
        if self.route_index_ref < 0:
            raise ValueError(
                f"route_index_ref 不能为负数: {self.route_index_ref}"
            )


@dataclass(frozen=True)
class CoarsePlanView:
    """
    RH-ALNS 向 PPO / candidate builder 输出的只读边界快照。

    含义：
      - 限定 PPO 当前能看见哪些订单；
      - 给出粗粒度优先级和回收候选边界；
      - 给出卡车骨架路线与参考 ETA；
      - 给出 planner 版本与有效期，用于局部动作失效和重规划管理。

    不包含：
      - 当前时刻精确 ETA 真值；
      - 电量是否真的足够；
      - 当前队列是否已超阈值；
      - 最终 action mask。
    """

    # 当前 coarse plan 的版本号；每次重规划后递增。
    plan_version: PlanVersion
    # 本版 coarse plan 的签发时刻（仿真秒）。
    issued_at: SimTimeSec
    # 本版 coarse plan 的理论有效截止时刻；超出后应考虑重规划。
    valid_until: SimTimeSec

    # 卡车未来骨架路线的固定节点序列，作为 mode C、launch trigger、route drift 的参考基线。
    # 仅包含未来会经过的固定交接节点（station / depot），不包含 customer。
    truck_backbone_route: tuple[NodeId, ...]
    # 卡车到骨架路线各固定节点的参考 ETA（仿真秒）。
    truck_eta_map: Mapping[NodeId, EtaSec]

    # 当前 coarse planner 放行给 PPO 的订单子集；PPO 只能在该集合内选订单。
    authorized_orders: tuple[OrderId, ...]
    # 订单的粗粒度优先级分桶结果，供候选集截断和解释性分析使用。
    order_priority_band: Mapping[OrderId, PriorityBand]
    # 订单的粗粒度预排序分数，供候选集按优先级裁剪时使用。
    order_pre_score: Mapping[OrderId, float]

    # 上层粗规划语义边界：某订单允许的配送模式集合，取值来自 {A, B, C}。
    planner_mode_cap: Mapping[OrderId, FrozenSet[PlannerMode]]
    # 真正暴露给 PPO actor 的订单级派送模式边界，取值来自 {B, C}。
    # 注意：WAIT 是全局动作，不挂在某个订单上，因此不出现在该字段中。
    policy_mode_mask: Mapping[OrderId, FrozenSet[PolicyMode]]

    # 每个订单在 coarse 层面允许考虑的回收节点池；最终合法性仍需运行时过滤。
    recovery_pool: Mapping[OrderId, tuple[NodeId, ...]]
    # 固定节点（station / depot）的充换电服务软预算；
    # 仅描述回收后进入节点充换电服务阶段的承压先验，不表示等待回收容量。
    node_charge_load_budget: Mapping[NodeId, NodeChargeLoadBudget]
    # 供 route drift 检查使用的参考基线：每个骨架节点的参考 ETA 与参考顺序位置。
    route_drift_ref: Mapping[NodeId, RouteDriftRef]
    # planner 层签发的 riding_with_truck 决策触发站点上界；
    # 运行时是否真正触发，仍需由 env_adapter 基于执行侧实时集合判定。
    launch_candidate_stations: tuple[NodeId, ...]
    # 可选退化开关：当为 True 时，允许 truck_backbone_route 为空，表示“卡车未来骨架已耗尽”。
    # 仅建议在 benchmark / hybrid 等不追加巡站循环的验证场景中显式开启；默认保持严格契约。
    allow_empty_backbone_route: bool = False

    # 缓存集合，便于高频 membership 查询。
    _authorized_order_set: FrozenSet[OrderId] = field(init=False, repr=False)
    _launch_station_set: FrozenSet[NodeId] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.plan_version < 0:
            raise ValueError(f"plan_version 不能为负数: {self.plan_version}")
        if self.issued_at < 0:
            raise ValueError(f"issued_at 不能为负数: {self.issued_at}")
        if self.valid_until < self.issued_at:
            raise ValueError(
                "valid_until 必须大于等于 issued_at: "
                f"{self.valid_until} < {self.issued_at}"
            )
        route_nodes = set(self.truck_backbone_route)
        if not route_nodes:
            if not self.allow_empty_backbone_route:
                raise ValueError(
                    "truck_backbone_route 不能为空；若需允许空骨架，"
                    "需显式开启 allow_empty_backbone_route"
                )
            if self.truck_eta_map:
                raise ValueError(
                    "allow_empty_backbone_route=True 时，truck_eta_map 必须为空"
                )
            if self.route_drift_ref:
                raise ValueError(
                    "allow_empty_backbone_route=True 时，route_drift_ref 必须为空"
                )
            if self.launch_candidate_stations:
                raise ValueError(
                    "allow_empty_backbone_route=True 且 truck_backbone_route 为空时，"
                    "launch_candidate_stations 必须为空"
                )
        else:
            if len(route_nodes) != len(self.truck_backbone_route):
                raise ValueError("truck_backbone_route 不允许重复节点")

            if missing_eta := route_nodes - set(self.truck_eta_map):
                raise ValueError(
                    "truck_eta_map 缺少骨架节点 ETA: "
                    f"{sorted(missing_eta)}"
                )
            if missing_drift := route_nodes - set(self.route_drift_ref):
                raise ValueError(
                    "route_drift_ref 缺少骨架节点参考项: "
                    f"{sorted(missing_drift)}"
                )
            if invalid_launch := set(self.launch_candidate_stations) - route_nodes:
                raise ValueError(
                    "launch_candidate_stations 必须来自 truck_backbone_route: "
                    f"{sorted(invalid_launch)}"
                )

        authorized_set = frozenset(self.authorized_orders)
        if len(authorized_set) != len(self.authorized_orders):
            raise ValueError("authorized_orders 不允许重复订单")

        # 验证所有 per-order 字段至少在被授权订单范围内自洽。
        for order_id in self.authorized_orders:
            if order_id not in self.order_priority_band:
                raise ValueError(
                    f"order_priority_band 缺少授权订单 {order_id}"
                )
            if order_id not in self.order_pre_score:
                raise ValueError(f"order_pre_score 缺少授权订单 {order_id}")
            if order_id not in self.planner_mode_cap:
                raise ValueError(
                    f"planner_mode_cap 缺少授权订单 {order_id}"
                )
            if order_id not in self.policy_mode_mask:
                raise ValueError(
                    f"policy_mode_mask 缺少授权订单 {order_id}"
                )
            if order_id not in self.recovery_pool:
                raise ValueError(f"recovery_pool 缺少授权订单 {order_id}")

        for order_id, modes in self.planner_mode_cap.items():
            if not modes:
                raise ValueError(f"planner_mode_cap[{order_id}] 不能为空")
            invalid = set(modes) - set(PlannerMode)
            if invalid:
                raise ValueError(
                    f"planner_mode_cap[{order_id}] 含非法模式: {invalid}"
                )

        for order_id, modes in self.policy_mode_mask.items():
            if not modes:
                raise ValueError(
                    f"policy_mode_mask[{order_id}] 不能为空"
                )
            invalid = set(modes) - set(_POLICY_TO_PLANNER_MODE)
            if invalid:
                raise ValueError(
                    f"policy_mode_mask[{order_id}] 只允许包含订单级派送模式 "
                    f"{{B, C}}，不应包含 WAIT: {invalid}"
                )

        for order_id in self.authorized_orders:
            planner_modes = self.planner_mode_cap[order_id]
            policy_modes = self.policy_mode_mask[order_id]

            allowed_policy_modes = set()
            for policy_mode, planner_mode in _POLICY_TO_PLANNER_MODE.items():
                if planner_mode in planner_modes:
                    allowed_policy_modes.add(policy_mode)

            if invalid_policy_modes := set(policy_modes) - allowed_policy_modes:
                raise ValueError(
                    f"policy_mode_mask[{order_id}] 越过 planner_mode_cap "
                    f"{sorted(mode.value for mode in planner_modes)}: "
                    f"{sorted(mode.value for mode in invalid_policy_modes)}"
                )

        if not route_nodes and self.allow_empty_backbone_route:
            nonempty_recovery_orders = [
                order_id for order_id, nodes in self.recovery_pool.items() if nodes
            ]
            if nonempty_recovery_orders:
                raise ValueError(
                    "allow_empty_backbone_route=True 且 truck_backbone_route 为空时，"
                    "recovery_pool 必须对所有订单为空: "
                    f"{sorted(nonempty_recovery_orders)}"
                )
            invalid_mode_c_orders = [
                order_id
                for order_id, modes in self.policy_mode_mask.items()
                if PolicyMode.C in modes
            ]
            if invalid_mode_c_orders:
                raise ValueError(
                    "allow_empty_backbone_route=True 且 truck_backbone_route 为空时，"
                    "policy_mode_mask 必须收缩为 {B}: "
                    f"{sorted(invalid_mode_c_orders)}"
                )

        for order_id, node_ids in self.recovery_pool.items():
            if len(set(node_ids)) != len(node_ids):
                raise ValueError(
                    f"recovery_pool[{order_id}] 不允许重复节点"
                )
            if invalid_nodes := set(node_ids) - route_nodes:
                raise ValueError(
                    f"recovery_pool[{order_id}] 含不在 truck_backbone_route "
                    f"中的节点: {sorted(invalid_nodes)}"
                )

        for node_id, eta in self.truck_eta_map.items():
            if eta < 0:
                raise ValueError(
                    f"truck_eta_map[{node_id}] 不能为负数: {eta}"
                )

        for node_id, budget in self.node_charge_load_budget.items():
            if budget < 0:
                raise ValueError(
                    f"node_charge_load_budget[{node_id}] 不能为负数: {budget}"
                )

        object.__setattr__(self, "_authorized_order_set", authorized_set)
        object.__setattr__(
            self, "_launch_station_set", frozenset(self.launch_candidate_stations)
        )

    @property
    def ttl_sec(self) -> float:
        """该 coarse plan 的理论生存时间。"""

        return self.valid_until - self.issued_at

    def is_order_authorized(self, order_id: OrderId) -> bool:
        """订单是否被当前 coarse plan 放行给 PPO。"""

        return order_id in self._authorized_order_set

    def is_launch_candidate_station(self, node_id: NodeId) -> bool:
        """该站点是否被当前 coarse plan 放入触发站点上界。"""

        return node_id in self._launch_station_set

    def get_planner_modes(self, order_id: OrderId) -> FrozenSet[PlannerMode]:
        """返回粗规划语义边界内允许的模式集合。"""

        return self.planner_mode_cap.get(order_id, frozenset())

    def get_policy_modes(self, order_id: OrderId) -> FrozenSet[PolicyMode]:
        """返回某订单可选的订单级派送模式集合（B/C，不含全局 WAIT）。"""

        return self.policy_mode_mask.get(order_id, frozenset())

    def get_recovery_candidates(self, order_id: OrderId) -> tuple[NodeId, ...]:
        """返回某订单在 coarse 层面允许考虑的回收节点池。"""

        return self.recovery_pool.get(order_id, ())

    def get_route_position(self, node_id: NodeId) -> int:
        """返回节点在 truck_backbone_route 中的参考顺序位置。"""

        return self.route_drift_ref[node_id].route_index_ref

    def get_route_eta_ref(self, node_id: NodeId) -> EtaSec:
        """返回节点在当前 coarse plan 中的参考 ETA。"""

        return self.route_drift_ref[node_id].eta_ref


# ── meta.json 契约 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyMeta:
    encoder_type: str
    d_model: int
    nhead: int
    ff_dim: int
    dropout: float
    lstm_hidden: int
    lstm_layers: int
    hist_len: int
    max_order_tokens: int
    max_recovery_tokens: int
    use_plan_version_delta: bool
    use_is_riding_truck_flag: bool
    use_drone_source_type_flag: bool
    critic_mode: str
    inference_mode: str

    def __post_init__(self) -> None:
        if self.d_model % self.nhead != 0:
            raise ValueError(
                f"d_model ({self.d_model}) 必须能被 nhead ({self.nhead}) 整除"
            )


@dataclass(frozen=True)
class ActionSpaceMeta:
    type: str
    factorized_head_order: tuple[str, ...]
    policy_modes: tuple[str, ...]
    planner_modes: tuple[str, ...]
    enable_wait_action: bool
    include_mode_a_in_policy: bool


@dataclass(frozen=True)
class CandidateMeta:
    max_candidate_orders: int
    max_candidate_recovery_per_order: int
    max_candidate_actions: int
    station_wait_threshold_sec: float
    rendezvous_eta_safe_margin_sec: float
    energy_safe_margin_ratio: float


@dataclass(frozen=True)
class PlannerMeta:
    coarse_replan_interval_sec: float
    coarse_new_order_trigger: int
    route_drift_trigger_ratio: float
    fallback_burst_trigger_count: int
    fallback_burst_window_sec: float
    hard_failure_trigger_count: int
    upper_horizon_sec: float
    support_radius_km: float
    min_orders_to_trigger: int


@dataclass(frozen=True)
class RewardMeta:
    lambda_wait: float
    wait_idle_penalty_coef: float
    lambda_queue: float
    lambda_miss: float
    lambda_res_timeout: float
    lambda_overdue: float
    R_delivery_bonus: float
    max_overdue_sec: float
    hard_overdue_penalty_sec: float
    hard_failure_penalty_sec: float
    primary_metrics_scope: str
    include_mode_a_in_primary_metrics: bool


@dataclass(frozen=True)
class EnvSemanticContractMeta:
    mode_c_recovery_nodes: tuple[str, ...]
    reservation_timeout_enabled: bool
    reservation_alpha: float
    reservation_beta: float
    reservation_gamma: float
    overdue_penalty_mode: str
    fifo_queue_enabled: bool
    riding_with_truck_enabled: bool
    allow_empty_backbone_route: bool
    hard_failure_type: str


@dataclass(frozen=True)
class OnlineLockParams:
    locked_fields: tuple[str, ...]
    tunable_fields: tuple[str, ...]


@dataclass(frozen=True)
class DroneRuntimeParamsSnapshot:
    k1: float
    k2: float
    cruise_speed: float
    payload_capacity: float
    empty_weight: float
    battery_capacity_j: float
    safe_margin_ratio: float


@dataclass(frozen=True)
class SolverEnergyRuntimeSnapshot:
    c_dist_et: float
    c_dist_uav: float
    c_energy_et: float
    c_energy_uav: float
    lambda_time: float
    truck_energy_kwh_per_km: float
    uav_energy_model: str
    uav_alpha_wh_per_kg_km: float
    allow_moving_truck_launch: bool
    truck_service_time_order_s: float
    drone_service_time_order_s: float
    truck_drone_launch_time_s: float
    truck_drone_recover_time_s: float


@dataclass(frozen=True)
class SharedRuntimeParamsSnapshot:
    """
    共享运行时参数快照，对应 `backend/config/drone_params.yaml`。
    用于锁定训练时采用的物理/服务时长语义。
    """

    source_config: str
    light_drone: DroneRuntimeParamsSnapshot
    heavy_drone: DroneRuntimeParamsSnapshot
    solver_energy: SolverEnergyRuntimeSnapshot

    def __post_init__(self) -> None:
        if not self.source_config:
            raise ValueError("source_config 不能为空")


@dataclass(frozen=True)
class MetaJson:
    """
    Phase 1 可冻结的结构参数契约。
    训练完成后与 TrainingRunMeta 合并，由 build_meta_json_dict() 序列化为 meta.json。
    """

    schema_version: str
    coarse_plan_view_contract_version: str
    env_semantic_contract_version: str
    policy: PolicyMeta
    action_space: ActionSpaceMeta
    candidate: CandidateMeta
    planner: PlannerMeta
    reward: RewardMeta
    shared_runtime_params_snapshot: SharedRuntimeParamsSnapshot
    env_semantic_contract: EnvSemanticContractMeta
    online_lock_params: OnlineLockParams

    def __post_init__(self) -> None:
        if not self.schema_version:
            raise ValueError("schema_version 不能为空")
        if not self.coarse_plan_view_contract_version:
            raise ValueError("coarse_plan_view_contract_version 不能为空")
        if not self.env_semantic_contract_version:
            raise ValueError("env_semantic_contract_version 不能为空")

    def to_dict(self) -> "dict[str, Any]":
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkMeta:
    """
    benchmark 身份快照。

    当前项目不强制单独维护 `benchmark_version` 字段，
    而是以固定订单源文件及其摘要作为 benchmark 身份：
      - `orders.json` 路径
      - 该文件整体 SHA256
      - `static_orders` / `dynamic_orders` 数量
      - benchmark 是否启用 `dynamic_orders`

    与 `TrainingInputMeta` 中的泊松参数、随机种子合并后，
    可唯一描述 benchmark / hybrid / poisson 三类订单源的确定性来源。
    """

    orders_json: str
    orders_json_sha256: str
    static_order_count: int
    dynamic_order_count: int
    benchmark_use_dynamic_orders: bool

    def __post_init__(self) -> None:
        if not self.orders_json:
            raise ValueError("orders_json 不能为空")
        if not self.orders_json_sha256:
            raise ValueError("orders_json_sha256 不能为空")


@dataclass(frozen=True)
class TrainingInputMeta:
    order_source_mode: str
    benchmark: BenchmarkMeta
    poisson_arrival_rate: float
    poisson_weight_max_kg: float
    order_window_min_min: int
    order_window_max_min: int
    poisson_seed: int
    training_seed: int
    total_timesteps: int


@dataclass(frozen=True)
class TrainingRunMeta:
    """
    训练完成后才能填充的运行时字段。
    与 MetaJson 合并，由 build_meta_json_dict() 序列化为 meta.json。
    """

    model_version: str
    trained_at: str
    scene_id: str
    scene_bundle_dir: str
    training_input: TrainingInputMeta

    def __post_init__(self) -> None:
        if not self.model_version:
            raise ValueError("model_version 不能为空")
        if not self.trained_at:
            raise ValueError("trained_at 不能为空")
        if not self.scene_id:
            raise ValueError("scene_id 不能为空")

    def to_dict(self) -> "dict[str, Any]":
        return asdict(self)


def build_meta_json_dict(
    meta: MetaJson, run: TrainingRunMeta
) -> "dict[str, Any]":
    """合并结构参数契约与训练运行时字段，生成完整的 meta.json 内容。"""
    return {**run.to_dict(), **meta.to_dict()}


__all__ = [
    "ActionSpaceMeta",
    "BenchmarkMeta",
    "CandidateMeta",
    "CoarsePlanView",
    "DroneRuntimeParamsSnapshot",
    "EnvSemanticContractMeta",
    "EtaSec",
    "MetaJson",
    "NodeChargeLoadBudget",
    "NodeId",
    "OnlineLockParams",
    "OrderId",
    "PlanVersion",
    "PlannerMeta",
    "PlannerMode",
    "PolicyMeta",
    "PolicyMode",
    "PriorityBand",
    "RewardMeta",
    "RouteDriftRef",
    "SharedRuntimeParamsSnapshot",
    "SimTimeSec",
    "SolverEnergyRuntimeSnapshot",
    "TrainingInputMeta",
    "TrainingRunMeta",
    "build_meta_json_dict",
]
