#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — Phase 5c 训练环境适配器。

职责边界：
  - 读取 Phase 2/3/4 产物，提供事件驱动的最小可运行训练环境；
  - 维护训练侧无人机状态机覆盖层，不污染 core 层物理枚举；
  - 在 Phase 5 内部内联实现最小 coarse plan / action mask；
  - 保证 reset -> step -> done 连通，且为 Phase 5b/5c 保留稳定接口。

本文件当前实现到 Phase 5c：
  - 完整 action_mask：mode C 候选按时序/能量精细过滤
  - 完整 post_delivery_revalidation：送达后对 mode C 原选回收点做三条件复核
  - mode B 返程宿主使用确定性 score 规则选择
  - reservation 状态机与 timeout 触发 fallback
  - 完整 per-dt reward：T_overdue / T_wait / T_queue / T_fallback
  - WAIT 动作的 T_idle 一次性精确结算
  - hard overdue 强制移除
"""

from __future__ import annotations

import copy
import json
import math
import random
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from core.entities.charging_host import ChargingHost
from core.entities.depot import Depot
from core.entities.drone import Drone
from core.entities.order import Order
from core.entities.primitives import Position3D, SourceType, TaskStatus
from core.entities.swap_station import SwapStation
from core.entities.truck import Truck
from environment.state.entity_manager import EntityManager
from environment.state.order_manager import OrderManager

from .actions import DispatchAction, EnvAction, GlobalWaitAction, WAIT_ACTION
from .candidate_builder import CandidateBuilder
from .contracts import (
    CandidateOutput,
    CoarsePlanView,
    DecisionExecutionSnapshot,
    DecisionPlannerSnapshot,
    PlannerMode,
    PlannerTriggerContext,
    PolicyMode,
    RouteDriftRef,
)
from .order_source_adapter import (
    OrderSourceConfig,
    OrderSourceMode,
    build_order_source,
    configure_order_manager_for_source,
)
from .planner_bridge import PlannerBridge
from .scene_loader import DEFAULT_CONFIG_PATH, TrainingSceneContext, load_default_scene


NodeId: TypeAlias = str
OrderId: TypeAlias = str
DroneId: TypeAlias = str

_TIME_EPS = 1e-6
_BACKBONE_DEPARTURE_EPS = 1e-6


class TrainingDroneState(StrEnum):
    """训练环境内部使用的无人机状态枚举。"""
    # 训练侧状态覆盖层；不修改 core 层 DroneStatus。
    IDLE = "idle"
    FLYING_TO_DELIVER = "flying_to_deliver"
    DELIVERY_SERVICE = "delivery_service"
    DELIVERED = "delivered"
    RETURN_TO_RENDEZVOUS = "return_to_rendezvous"
    WAITING_FOR_TRUCK = "waiting_for_truck"
    RETURN_TO_STATION = "return_to_station"
    RETURN_TO_DEPOT = "return_to_depot"
    QUEUEING_AT_HOST = "queueing_at_host"
    CHARGING_OR_SWAP = "charging_or_swap"
    ACTIVE_WAIT = "active_wait"
    FALLBACK_RECOVERY = "fallback_recovery"
    CHARGING_ON_TRUCK = "charging_on_truck"
    RIDING_WITH_TRUCK = "riding_with_truck"
    AIRBORNE_ENERGY_FAILURE = "airborne_energy_failure"


@dataclass(frozen=True)
class ReservationStateView:
    """reservation 只读视图。
    """
    recover_node: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class ReservationState:
    """训练环境内部维护的 mode C reservation 真值。"""
    recover_node: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class DroneStateView:
    """单架无人机在 runtime_state 中的只读快照。"""
    drone_id: str
    training_state: str
    current_loc: Position3D
    battery_current: float
    battery_max: float
    battery_ratio: float
    carrying_order_id: str | None
    home_type: str
    cruise_speed: float
    payload_capacity: float
    empty_weight: float
    k1: float
    k2: float
    reservation: ReservationStateView | None


@dataclass(frozen=True)
class NodeStateView:
    """固定充换电节点在 runtime_state 中的只读快照。"""
    node_id: str
    node_type: str
    position: Position3D
    parking_slots: int
    swap_time: float
    queue_length: int
    available_slots: int


@dataclass(frozen=True)
class RuntimeStateView:
    """当前时刻暴露给训练侧的运行时状态快照。"""
    # Phase 5 已固定对外 schema；后续阶段只能填充实现，不能改字段形状。
    t_now: float
    truck_current_loc: Position3D
    drone_states: Mapping[str, DroneStateView]
    pending_orders: Mapping[str, Order]
    assigned_orders: Mapping[str, Order]
    node_states: Mapping[str, NodeStateView]
    reservation_count: Mapping[str, int]


@dataclass(frozen=True)
class DecisionContext:
    """一次 PPO 决策点对应的上下文。"""
    # 当前实现只返回 action_lookup；还没有单独暴露 dense action_mask 张量。
    t_decision: float
    deciding_drone_id: str
    trigger_type: str
    trigger_station_id: str | None
    runtime_state: RuntimeStateView
    coarse_plan: CoarsePlanView
    planner_snapshot: DecisionPlannerSnapshot
    execution_snapshot: DecisionExecutionSnapshot
    action_lookup: tuple[EnvAction, ...]


@dataclass(frozen=True)
class EnvStepResult:
    """一次 `reset()` 或 `step()` 调用返回的统一结果对象。"""
    reward: float
    done: bool
    runtime_state: RuntimeStateView
    decision_context: DecisionContext | None
    info: Mapping[str, Any]


@dataclass(frozen=True)
class FallbackLeg:
    """fallback_recovery 执行段的内部账本。"""
    # fallback_recovery 的退出完全依赖这份账本；到达宿主后必须清空。
    host_node_id: str
    host_node_type: str
    arrival_time: float


@dataclass(frozen=True)
class BackboneVisit:
    """卡车对固定节点的一次未来访问记录。"""
    # _full_backbone_cache 的单条访问记录；允许同一 node_id 多次出现。
    node_id: str
    arrival_time: float
    departure_time: float


@dataclass(frozen=True)
class PlannedStop:
    """卡车事件队列中的一个停靠点。"""
    # 统一承接 Phase 4 stops 和 poisson 巡站追加停靠点。
    seq: int
    node_type: str
    node_id: str
    position: Position3D
    order_id: str | None
    arrival_time: float
    departure_time: float


@dataclass(frozen=True)
class FlightLeg:
    """无人机当前正在执行的一段飞行。"""
    # 当前实现把所有空中过程统一收敛为绝对时刻的飞行段，事件推进只看 arrival_time。
    kind: str
    start_time: float
    arrival_time: float
    start_pos: Position3D
    target_pos: Position3D
    order_id: str | None = None
    target_node_id: str | None = None
    target_node_type: str | None = None
    energy_cost_j: float = 0.0


@dataclass(frozen=True)
class DeliveryServiceLeg:
    """无人机送达客户后在客户点执行的显式服务停留。"""
    order_id: str
    start_time: float
    finish_time: float
    service_pos: Position3D


@dataclass(frozen=True)
class DispatchCommit:
    """记录某架无人机当前订单的 dispatch 承诺。"""
    order_id: str
    mode: PolicyMode
    selected_recover_node: str | None
    trigger_station_id: str | None


@dataclass(frozen=True)
class DecisionTrigger:
    """内部决策队列中的一个待处理触发点。"""
    drone_id: str
    trigger_type: str
    trigger_station_id: str | None = None


@dataclass(frozen=True)
class _YamlConfig:
    """从训练 YAML 中提取出的 5b 运行参数子集。"""
    upper_horizon_sec: float
    patrol_min_remaining_sec: float
    patrol_stations_per_loop: int
    allow_empty_backbone_route: bool
    max_wait_decision_gap_sec: float
    max_candidate_recovery_per_order: int
    rendezvous_eta_safe_margin_sec: float
    reservation_enabled: bool
    reservation_alpha: float
    reservation_beta: float
    reservation_gamma: float
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
    support_radius_km: float
    fallback_burst_window_sec: float
    coarse_new_order_trigger: int


class TrainingEnvAdapter:
    """
    Phase 5b 训练环境。

    当前只支持单 truck / 单 depot 场景，与 Phase 4 路线导出约束保持一致。
    """

    def __init__(
        self,
        *,
        scene_ctx: TrainingSceneContext | None = None,
        order_source: OrderSourceConfig | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        phase4_route_dir: str | Path | None = None,
        enable_phase6: bool = True,
        planner_bridge: PlannerBridge | None = None,
        candidate_builder: CandidateBuilder | None = None,
    ) -> None:
        """构造环境对象并装载静态依赖。

        这里只准备配置、scene 和订单源；真正的 episode 运行态在 `reset()` 里创建。
        """
        self._config_path = Path(config_path)
        self._cfg = _load_env_yaml(self._config_path)

        self._scene_ctx = scene_ctx or load_default_scene(config_path=self._config_path)
        self._order_source = order_source or build_order_source(
            self._scene_ctx,
            config_path=self._config_path,
        )
        self._candidate_builder = candidate_builder or CandidateBuilder(
            scene_ctx=self._scene_ctx,
            config_path=self._config_path,
        )
        self._phase4_route_dir = (
            Path(phase4_route_dir)
            if phase4_route_dir is not None
            else Path(self._scene_ctx.scene_bundle_dir) / "sumo" / "phase4_truck_route"
        )

        self._entity_manager: EntityManager | None = None
        self._order_manager: OrderManager | None = None
        self._truck: Truck | None = None
        self._truck_id: str | None = None
        self._depot: Depot | None = None
        self._depot_id: str | None = None
        self._heavy_payload_capacity = self._resolve_heavy_payload_capacity()

        self._t_now = 0.0
        self._reset_count = 0
        self._current_episode_order_source_seed = int(self._order_source.seed)
        self._planned_route_stops: list[PlannedStop] = []
        self._planned_route_stop_i = 1
        self._full_backbone_cache: list[BackboneVisit] = []
        self._allow_empty_backbone_route = False

        self._drone_state: dict[DroneId, TrainingDroneState] = {}
        # 记录 active_wait 结束后应恢复到哪个基础状态。
        # idle 路径仍在 step(WAIT) 入口一次性结算；riding_with_truck 路径按真实 dt 累计 T_idle。
        self._active_wait_resume: dict[DroneId, TrainingDroneState] = {}
        self._flight_legs: dict[DroneId, FlightLeg] = {}
        self._delivery_service_legs: dict[DroneId, DeliveryServiceLeg] = {}
        self._fallback_leg: dict[DroneId, FallbackLeg] = {}
        # 记录当前订单对应的 dispatch 承诺，供 delivered 后执行层继续转移。
        self._dispatch_commit: dict[DroneId, DispatchCommit] = {}
        self._reservations: dict[DroneId, ReservationState] = {}
        self._reservation_count: dict[NodeId, int] = {}
        # 车载充换电完成时刻；到点后 charging_on_truck -> riding_with_truck。
        self._truck_charge_until: dict[DroneId, float] = {}
        self._decision_queue: list[DecisionTrigger] = []

        # 只跟踪 truck_execution_route 中的 mode A 背景订单完成情况。
        self._background_mode_a_order_count = 0
        self._background_mode_a_pending: set[str] = set()
        self._background_mode_a_completed: set[str] = set()
        self._background_mode_a_completion_time_sum = 0.0
        self._truck_background_order_completion_events: list[dict[str, Any]] = []
        self._last_reward_breakdown: dict[str, float] = {}
        # 每个无人机自上次决策以来累积的归因成本（方案一强化版）。
        # key = drone_id，value = 该无人机应承担的负奖励总量（已含符号，即负值）。
        self._agent_cost_accum: dict[DroneId, float] = {}
        self._current_coarse_plan: CoarsePlanView | None = None
        self._active_launch_stations: set[str] = set()
        self._fallback_event_times: list[float] = []
        self._hard_failure_event_times: list[float] = []
        self._completed_backbone_nodes_since_plan: set[str] = set()
        self._episode_delivery_count = 0
        self._episode_fallback_count = 0
        self._episode_hard_failure_count = 0
        self._episode_reservation_timeout_count = 0
        self._episode_hard_overdue_count = 0
        self._episode_wait_action_count = 0
        self._episode_dispatch_mode_b_count = 0
        self._episode_dispatch_mode_c_count = 0
        self._episode_wait_time_sec = 0.0
        self._episode_idle_time_sec = 0.0
        self._episode_queue_time_sec = 0.0
        self._episode_fallback_time_sec = 0.0
        self._episode_overdue_time_sec = 0.0
        self._episode_reservation_timeout_cost_sec = 0.0
        self._planner_bridge = (
            planner_bridge
            if planner_bridge is not None
            else (
                PlannerBridge(
                    future_backbone_provider=self._future_backbone_visits,
                    config_path=self._config_path,
                    heavy_payload_capacity=self._heavy_payload_capacity,
                )
                if enable_phase6
                else None
            )
        )

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def reset(self) -> EnvStepResult:
        """重建一个全新的 episode，并返回首个可见状态。

        当前实现会：
          1. 重建实体与订单管理器；
          2. 读取 Phase 4 产物并按需追加 poisson 巡站循环；
          3. 初始化训练侧状态机；
          4. 注入 t=0 的订单并生成初始决策点。
        """
        episode_seed = int(self._order_source.seed)
        if self._order_source.mode == OrderSourceMode.POISSON:
            episode_seed += int(self._reset_count)
            random.seed(episode_seed)
        else:
            random.seed(episode_seed)
        self._current_episode_order_source_seed = episode_seed

        self._entity_manager = EntityManager()
        self._entity_manager.load_from_config({"entities": self._scene_ctx.entities_raw})
        self._order_manager = OrderManager()
        configure_order_manager_for_source(
            self._order_manager,
            self._scene_ctx,
            self._order_source,
        )

        self._truck_id, self._truck = _require_singleton(self._entity_manager.trucks, "truck")
        self._depot_id, self._depot = _require_singleton(self._entity_manager.depots, "depot")

        self._t_now = 0.0
        self._planned_route_stop_i = 1
        self._flight_legs.clear()
        self._delivery_service_legs.clear()
        self._fallback_leg.clear()
        self._dispatch_commit.clear()
        self._reservations.clear()
        self._reservation_count.clear()
        self._truck_charge_until.clear()
        self._decision_queue.clear()
        self._active_wait_resume.clear()
        self._background_mode_a_order_count = 0
        self._background_mode_a_pending.clear()
        self._background_mode_a_completed.clear()
        self._background_mode_a_completion_time_sum = 0.0
        self._truck_background_order_completion_events.clear()
        self._last_reward_breakdown = {}
        self._agent_cost_accum.clear()
        self._current_coarse_plan = None
        self._active_launch_stations.clear()
        self._fallback_event_times.clear()
        self._hard_failure_event_times.clear()
        self._completed_backbone_nodes_since_plan.clear()
        self._episode_delivery_count = 0
        self._episode_fallback_count = 0
        self._episode_hard_failure_count = 0
        self._episode_reservation_timeout_count = 0
        self._episode_hard_overdue_count = 0
        self._episode_wait_action_count = 0
        self._episode_dispatch_mode_b_count = 0
        self._episode_dispatch_mode_c_count = 0
        self._episode_wait_time_sec = 0.0
        self._episode_idle_time_sec = 0.0
        self._episode_queue_time_sec = 0.0
        self._episode_fallback_time_sec = 0.0
        self._episode_overdue_time_sec = 0.0
        self._episode_reservation_timeout_cost_sec = 0.0

        artifacts = self._load_phase4_artifacts()
        self._planned_route_stops = list(artifacts["planned_stops"])
        self._full_backbone_cache = list(artifacts["backbone_cache"])
        self._background_mode_a_order_count = len(artifacts["mode_a_order_ids"])
        self._background_mode_a_pending = set(artifacts["mode_a_order_ids"])

        if self._order_source.mode == OrderSourceMode.POISSON:
            # 仅 poisson 训练模式追加巡站循环，benchmark/hybrid 保持 Phase 4 原路线。
            self._append_patrol_loop_if_needed()
            self._allow_empty_backbone_route = False
        else:
            self._allow_empty_backbone_route = True
        if self._planner_bridge is not None:
            self._planner_bridge.reset_episode(
                allow_empty_backbone_route=self._allow_empty_backbone_route
            )

        self._bind_truck_route()
        self._initialize_drone_states()

        # 允许 poisson 在 t=0 生成首单，使 reset 后的初始决策上下文可见新单。
        self._order_manager.tick(0.0, self._entity_manager)
        self._enqueue_initial_idle_decisions()
        if not self._decision_queue:
            self._advance_until_decision_or_done()

        self._reset_count += 1
        return self._build_step_result(reward=0.0, info={"event": "reset"})

    def step(self, action: EnvAction) -> EnvStepResult:
        """消费当前决策点的一个动作，并推进到下一个决策点或 episode 结束。

        奖励归因（方案一强化版）：
          - 先取走当前决策无人机自“上一次它自己决策”以来累计的 carry-in 奖励；
          - 再结算本次动作窗口内该无人机新产生的归因成本；
          - 最终 PPO reward = carry-in + 本次动作窗口新增奖励；
          - 其他无人机在同一时间窗口内产生的成本留在各自的累积器中，
            等到它们自己的决策点时再被取走。
        """
        if self.is_done():
            raise RuntimeError("episode 已结束，不能继续 step()")
        if not self._decision_queue:
            raise RuntimeError("当前没有可执行的 decision context")

        trigger = self._decision_queue.pop(0)
        deciding_drone_id = trigger.drone_id
        decision = self._build_decision_context(trigger)
        if not self._is_action_allowed(action, decision.action_lookup):
            raise ValueError(f"非法动作: {action}")

        # 方案一强化版的关键语义：
        # 当前 drone 在别人动作窗口内累计的自身成本，不应在这里丢失，而应在它自己
        # 下一次决策时以 carry-in 的形式被取走。
        carried_reward = self._agent_cost_accum.pop(deciding_drone_id, 0.0)
        reward_breakdown: dict[str, float] = {}
        self._last_reward_breakdown = {}
        info: dict[str, Any] = {
            "drone_id": deciding_drone_id,
            "trigger_type": trigger.trigger_type,
            "trigger_station_id": trigger.trigger_station_id,
        }

        if isinstance(action, GlobalWaitAction):
            self._episode_wait_action_count += 1
            current_state = self._drone_state[deciding_drone_id]
            info["applied_action"] = "WAIT"
            if current_state == TrainingDroneState.IDLE:
                delta_wait = self._compute_wait_delta(deciding_drone_id)
                self._apply_wait_action(deciding_drone_id)
                info["wait_delta"] = delta_wait
                # T_idle（IDLE 路径）：一次性精确结算，累加进累积器（保留跨区间已积累的成本）
                idle_penalty = -self._cfg.wait_idle_penalty_coef * delta_wait
                self._agent_cost_accum[deciding_drone_id] = (
                    self._agent_cost_accum.get(deciding_drone_id, 0.0) + idle_penalty
                )
                self._advance_to_event(self._t_now + delta_wait)
                _merge_reward_breakdown(reward_breakdown, self._last_reward_breakdown)
                self._resume_capped_wait_if_needed(deciding_drone_id)
                if not self._decision_queue and not self.is_done():
                    self._advance_until_decision_or_done()
                    _merge_reward_breakdown(reward_breakdown, self._last_reward_breakdown)
            elif current_state == TrainingDroneState.RIDING_WITH_TRUCK:
                self._apply_wait_action(deciding_drone_id)
                info["wait_mode"] = "deferred_riding_with_truck"
                self._advance_until_decision_or_done()
                _merge_reward_breakdown(reward_breakdown, self._last_reward_breakdown)
            else:
                raise RuntimeError(f"状态 {current_state.value} 不允许执行 WAIT")
        else:
            if action.mode == PolicyMode.B:
                self._episode_dispatch_mode_b_count += 1
            elif action.mode == PolicyMode.C:
                self._episode_dispatch_mode_c_count += 1
            self._apply_dispatch_action(trigger, action)
            info["applied_action"] = {
                "order_id": action.order_id,
                "mode": action.mode.value,
                "recover_node_id": action.recover_node_id,
            }
            if not self._decision_queue:
                self._advance_until_decision_or_done()
                _merge_reward_breakdown(reward_breakdown, self._last_reward_breakdown)

        # 本次 transition 的 reward 由两部分组成：
        # 1. carry-in：该无人机在别人动作期间已累计、延迟到本次决策点取走的成本；
        # 2. post-action：该无人机在本次动作窗口内新产生的归因成本。
        post_action_reward = self._agent_cost_accum.pop(deciding_drone_id, 0.0)
        reward = carried_reward + post_action_reward
        reward_breakdown["attributed_carry_in"] = carried_reward
        reward_breakdown["attributed_post_action"] = post_action_reward
        reward_breakdown["attributed_total"] = reward

        self._last_reward_breakdown = reward_breakdown

        return self._build_step_result(reward=reward, info=info)

    def is_done(self) -> bool:
        """检查 episode 是否满足当前实现中的任一终止条件。"""
        if self._t_now >= self._cfg.upper_horizon_sec - _TIME_EPS:
            return True
        if (
            self._order_manager is not None
            and not self._order_manager.pending_orders
            and not self._order_manager.assigned_orders
            and not self._background_mode_a_pending
        ):
            return True
        if self._drone_state and all(
            state == TrainingDroneState.AIRBORNE_ENERGY_FAILURE
            for state in self._drone_state.values()
        ):
            return True
        return False

    def current_episode_order_source_seed(self) -> int:
        """返回当前 episode 实际使用的订单源随机种子。"""
        return int(self._current_episode_order_source_seed)

    def consume_terminal_agent_costs(self) -> dict[str, float]:
        """在 episode 结束后取走各无人机尚未结算的尾部归因成本。"""
        if not self.is_done():
            raise RuntimeError("episode 尚未结束，不能消费 terminal agent costs")
        pending = {
            str(drone_id): float(reward)
            for drone_id, reward in self._agent_cost_accum.items()
        }
        self._agent_cost_accum.clear()
        return pending

    @property
    def current_decision_context(self) -> DecisionContext | None:
        """返回队首决策点对应的上下文；若当前无需决策则返回 `None`。"""
        if not self._decision_queue or self.is_done():
            return None
        return self._build_decision_context(self._decision_queue[0])

    def build_candidate_output(
        self,
        decision_context: DecisionContext,
        *,
        last_seen_plan_version: int,
    ) -> CandidateOutput:
        """基于一次既有决策快照重建外部可消费的 `CandidateOutput`。

        该入口显式保持：
          - `CandidateOutput` 仍由 env 外部调用方持有；
          - 重建所依据的状态快照来自 `decision_context`，而不是 env 当前真值；
          - 与兼容字段 `DecisionContext.action_lookup` 的可执行动作集合保持一致。
        """
        candidate_out = self._candidate_builder.build_from_decision_context(
            decision_context,
            last_seen_plan_version=last_seen_plan_version,
        )
        rebuilt_action_lookup = candidate_out.resolved_action_lookup.as_action_lookup()
        if rebuilt_action_lookup != decision_context.action_lookup:
            raise RuntimeError(
                "CandidateOutput 与 DecisionContext.action_lookup 不一致；"
                "请检查 decision_context 是否由同一 env / candidate_builder 生成"
            )
        return candidate_out

    # ---------------------------------------------------------------------
    # Core builders
    # ---------------------------------------------------------------------

    def build_runtime_state_view(self) -> RuntimeStateView:
        """基于当前内部真值构造一次 runtime_state 快照。"""
        entity_mgr = self._require_entity_manager()
        order_mgr = self._require_order_manager()
        truck = self._require_truck()

        drone_states = {
            drone_id: DroneStateView(
                drone_id=drone_id,
                training_state=self._drone_state[drone_id].value,
                current_loc=_clone_position(drone.current_loc),
                battery_current=self._effective_battery_current(drone_id, self._t_now),
                battery_max=float(drone.battery_max),
                battery_ratio=(
                    self._effective_battery_current(drone_id, self._t_now)
                    / max(_TIME_EPS, float(drone.battery_max))
                ),
                carrying_order_id=drone.carrying_order_id,
                home_type=drone.home_type.value,
                cruise_speed=float(drone.cruise_speed),
                payload_capacity=float(drone.payload_capacity),
                empty_weight=float(drone.empty_weight),
                k1=float(drone.k1),
                k2=float(drone.k2),
                reservation=(
                    ReservationStateView(
                        recover_node=reservation.recover_node,
                        issued_at=float(reservation.issued_at),
                        expires_at=float(reservation.expires_at),
                    )
                    if (reservation := self._reservations.get(drone_id)) is not None
                    else None
                ),
            )
            for drone_id, drone in entity_mgr.drones.items()
        }

        node_states: dict[str, NodeStateView] = {}
        for node_id, station in entity_mgr.stations.items():
            node_states[node_id] = NodeStateView(
                node_id=node_id,
                node_type="station",
                position=_clone_position(station.location),
                parking_slots=int(station.parking_slots),
                swap_time=float(station.swap_time),
                queue_length=int(station.queue_length),
                available_slots=int(station.available_slots),
            )
        for node_id, depot in entity_mgr.depots.items():
            node_states[node_id] = NodeStateView(
                node_id=node_id,
                node_type="depot",
                position=_clone_position(depot.location),
                parking_slots=int(depot.parking_slots),
                swap_time=float(depot.swap_time),
                queue_length=int(depot.queue_length),
                available_slots=int(depot.available_slots),
            )

        return RuntimeStateView(
            t_now=float(self._t_now),
            truck_current_loc=_clone_position(truck.current_loc),
            drone_states=drone_states,
            pending_orders=copy.copy(order_mgr.pending_orders),
            assigned_orders=copy.copy(order_mgr.assigned_orders),
            node_states=node_states,
            reservation_count=copy.copy(self._reservation_count),
        )

    def build_system_context_stats(self) -> dict[str, Any]:
        """返回不进入 PPO 主奖励的系统上下文统计。"""
        return {
            "mode_a_background_order_count": int(self._background_mode_a_order_count),
            "mode_a_background_completed_count": len(self._background_mode_a_completed),
            "mode_a_background_pending_count": len(self._background_mode_a_pending),
            "mode_a_background_completion_time_sum": float(
                self._background_mode_a_completion_time_sum
            ),
            "truck_background_order_completion_events": tuple(
                dict(event) for event in self._truck_background_order_completion_events
            ),
        }

    def build_episode_metrics_snapshot(self) -> dict[str, Any]:
        """返回当前 episode 的聚合指标快照。"""
        order_mgr = self._require_order_manager()
        completed_primary_orders = [
            order
            for order in order_mgr.completed_orders
            if self._is_uav_primary_order(order)
        ]
        delivered_primary_orders = [
            order
            for order in completed_primary_orders
            if order.status == TaskStatus.COMPLETED
        ]
        timed_out_primary_orders = [
            order
            for order in completed_primary_orders
            if order.status == TaskStatus.TIMEOUT
        ]
        on_time_delivery_count = sum(
            1
            for order in delivered_primary_orders
            if order.actual_deliver_time is not None
            and float(order.actual_deliver_time) <= float(order.deadline) + _TIME_EPS
        )
        overdue_delivery_count = len(delivered_primary_orders) - on_time_delivery_count
        on_time_rate = (
            float(on_time_delivery_count) / float(len(delivered_primary_orders))
            if delivered_primary_orders
            else 0.0
        )
        return {
            "done_reason": self._episode_done_reason(),
            "episode_end_t_sec": float(self._t_now),
            "delivery_count": int(self._episode_delivery_count),
            "on_time_delivery_count": int(on_time_delivery_count),
            "overdue_delivery_count": int(overdue_delivery_count),
            "on_time_rate": float(on_time_rate),
            "timeout_order_count": int(len(timed_out_primary_orders)),
            "fallback_count": int(self._episode_fallback_count),
            "hard_failure_count": int(self._episode_hard_failure_count),
            "reservation_timeout_count": int(self._episode_reservation_timeout_count),
            "hard_overdue_count": int(self._episode_hard_overdue_count),
            "wait_action_count": int(self._episode_wait_action_count),
            "dispatch_mode_b_count": int(self._episode_dispatch_mode_b_count),
            "dispatch_mode_c_count": int(self._episode_dispatch_mode_c_count),
            "t_wait_sec": float(self._episode_wait_time_sec),
            "t_idle_sec": float(self._episode_idle_time_sec),
            "t_queue_sec": float(self._episode_queue_time_sec),
            "t_fallback_sec": float(self._episode_fallback_time_sec),
            "t_overdue_sec": float(self._episode_overdue_time_sec),
            "t_reservation_timeout_cost_sec": float(
                self._episode_reservation_timeout_cost_sec
            ),
            "pending_primary_order_count": int(
                sum(
                    1
                    for order in order_mgr.pending_orders.values()
                    if self._is_uav_primary_order(order)
                )
            ),
            "assigned_primary_order_count": int(
                sum(
                    1
                    for order in order_mgr.assigned_orders.values()
                    if self._is_uav_primary_order(order)
                )
            ),
            "system_context_stats": self.build_system_context_stats(),
        }

    def build_visualization_snapshot(self) -> dict[str, Any]:
        """返回供实时可视化使用的轻量级状态快照。"""
        runtime_state = self.build_runtime_state_view()
        order_mgr = self._require_order_manager()
        truck = self._require_truck()
        decision = self.current_decision_context

        visual_order_by_id: dict[str, dict[str, Any]] = {}
        for order in self._scene_ctx.static_orders:
            visual_order_by_id[str(order.order_id)] = self._serialize_visual_order(order)

        active_orders = list(runtime_state.pending_orders.values()) + list(runtime_state.assigned_orders.values())
        for order in active_orders:
            visual_order_by_id[str(order.order_id)] = self._serialize_visual_order(order)
        for order in order_mgr.completed_orders:
            visual_order_by_id[str(order.order_id)] = self._serialize_visual_order(order)
        orders = sorted(
            visual_order_by_id.values(),
            key=lambda item: (float(item["create_time"]), item["order_id"]),
        )

        drones = [
            {
                "drone_id": drone_state.drone_id,
                "status": drone_state.training_state,
                "x": float(drone_state.current_loc.x),
                "y": float(drone_state.current_loc.y),
                "z": float(drone_state.current_loc.z),
                "battery_ratio": float(drone_state.battery_ratio),
                "battery_current": float(drone_state.battery_current),
                "battery_max": float(drone_state.battery_max),
                "carrying_order_id": drone_state.carrying_order_id,
                "reservation_node_id": (
                    drone_state.reservation.recover_node
                    if drone_state.reservation is not None
                    else None
                ),
            }
            for drone_state in sorted(
                runtime_state.drone_states.values(),
                key=lambda item: item.drone_id,
            )
        ]

        return {
            "t_now": float(runtime_state.t_now),
            "truck": {
                "truck_id": str(self._truck_id or ""),
                "x": float(truck.current_loc.x),
                "y": float(truck.current_loc.y),
                "z": float(truck.current_loc.z),
            },
            "drones": drones,
            "orders": orders,
            "current_decision": (
                {
                    "drone_id": str(decision.deciding_drone_id),
                    "trigger_type": str(decision.trigger_type),
                    "trigger_station_id": decision.trigger_station_id,
                }
                if decision is not None
                else None
            ),
            "last_reward_breakdown": dict(self._last_reward_breakdown),
        }

    def _build_coarse_plan_view(self, t_now: float) -> CoarsePlanView:
        """在给定时刻构造 Phase 5 内联 coarse plan。

        当前版本不做重规划，`plan_version` 固定为 0。
        """
        order_mgr = self._require_order_manager()

        deduped = list(self._dedup_backbone_visits(self._future_backbone_visits(t_now)))

        truck_backbone_route = tuple(visit.node_id for visit in deduped)
        if not truck_backbone_route and not self._allow_empty_backbone_route:
            raise RuntimeError("poisson 模式下不允许空骨架，请检查 patrol loop 生成逻辑")

        truck_eta_map = {visit.node_id: float(visit.arrival_time) for visit in deduped}
        route_drift_ref = {
            visit.node_id: RouteDriftRef(
                eta_ref=float(visit.arrival_time),
                route_index_ref=idx,
            )
            for idx, visit in enumerate(deduped)
        }
        launch_candidate_stations = tuple(
            node_id
            for node_id in truck_backbone_route
            if node_id in self._require_entity_manager().stations
        )

        authorized_orders: list[str] = []
        order_priority_band: dict[str, int] = {}
        order_pre_score: dict[str, float] = {}
        planner_mode_cap: dict[str, frozenset[PlannerMode]] = {}
        policy_mode_mask: dict[str, frozenset[PolicyMode]] = {}
        recovery_pool: dict[str, tuple[str, ...]] = {}

        for order_id, order in sorted(
            order_mgr.pending_orders.items(),
            key=lambda item: (item[1].deadline, item[0]),
        ):
            # 超过 heavy payload 上限的订单只保留 planner 语义边界，不进入 UAV 授权集合。
            if float(order.payload_weight) > self._heavy_payload_capacity:
                planner_mode_cap[order_id] = frozenset({PlannerMode.A})
                continue

            remaining = max(0.0, float(order.deadline) - t_now)
            time_window = max(_TIME_EPS, float(order.time_window_seconds))
            ratio = remaining / time_window
            if ratio <= 1.0 / 3.0:
                band = 0
            elif ratio <= 2.0 / 3.0:
                band = 1
            else:
                band = 2

            authorized_orders.append(order_id)
            order_priority_band[order_id] = band
            order_pre_score[order_id] = remaining
            planner_mode_cap[order_id] = frozenset({PlannerMode.B, PlannerMode.C})

            if not truck_backbone_route and self._allow_empty_backbone_route:
                policy_mode_mask[order_id] = frozenset({PolicyMode.B})
                recovery_pool[order_id] = ()
            else:
                policy_mode_mask[order_id] = frozenset({PolicyMode.B, PolicyMode.C})
                # coarse plan 只暴露 recovery pool；最终动作合法性由 5b 运行时过滤负责。
                recovery_pool[order_id] = truck_backbone_route[: self._cfg.max_candidate_recovery_per_order]

        node_charge_load_budget = {
            **{node_id: 0 for node_id in self._require_entity_manager().stations},
            **{node_id: 0 for node_id in self._require_entity_manager().depots},
        }

        return CoarsePlanView(
            plan_version=0,
            issued_at=float(t_now),
            valid_until=float(self._cfg.upper_horizon_sec),
            truck_backbone_route=truck_backbone_route,
            truck_eta_map=truck_eta_map,
            authorized_orders=tuple(authorized_orders),
            order_priority_band=order_priority_band,
            order_pre_score=order_pre_score,
            planner_mode_cap=planner_mode_cap,
            policy_mode_mask=policy_mode_mask,
            recovery_pool=recovery_pool,
            node_charge_load_budget=node_charge_load_budget,
            route_drift_ref=route_drift_ref,
            launch_candidate_stations=launch_candidate_stations,
            allow_empty_backbone_route=self._allow_empty_backbone_route,
        )

    def _future_backbone_visits(self, t_now: float) -> tuple[BackboneVisit, ...]:
        """返回当前时刻之后仍在未来骨架中的固定节点访问记录。"""
        return tuple(
            visit
            for visit in self._full_backbone_cache
            if visit.arrival_time > t_now + _TIME_EPS
        )

    def _dedup_backbone_visits(
        self,
        visits: tuple[BackboneVisit, ...],
    ) -> tuple[BackboneVisit, ...]:
        deduped: list[BackboneVisit] = []
        seen_nodes: set[str] = set()
        for visit in visits:
            if visit.node_id in seen_nodes:
                continue
            seen_nodes.add(visit.node_id)
            deduped.append(visit)
        return tuple(deduped)

    def _refresh_coarse_plan_if_needed(
        self,
        runtime_state: RuntimeStateView | None = None,
    ) -> CoarsePlanView:
        """统一 coarse plan 刷新入口。"""
        if self._planner_bridge is None:
            return self._build_coarse_plan_view(self._t_now)

        if runtime_state is None:
            runtime_state = self.build_runtime_state_view()

        trigger_ctx = self._build_planner_trigger_context(runtime_state)
        coarse_plan = self._planner_bridge.maybe_replan(runtime_state, trigger_ctx)
        if (
            self._current_coarse_plan is None
            or coarse_plan.plan_version > self._current_coarse_plan.plan_version
        ):
            self._current_coarse_plan = coarse_plan
            self._active_launch_stations = set(coarse_plan.launch_candidate_stations)
            self._completed_backbone_nodes_since_plan.clear()

        self._prune_active_launch_stations(runtime_state)
        return self._current_coarse_plan

    def _build_planner_trigger_context(
        self,
        runtime_state: RuntimeStateView,
    ) -> PlannerTriggerContext:
        metrics = self._compute_planner_snapshot_metrics(runtime_state)
        self._prune_window_events(self._fallback_event_times, runtime_state.t_now)
        self._prune_window_events(self._hard_failure_event_times, runtime_state.t_now)
        return PlannerTriggerContext(
            t_now=float(runtime_state.t_now),
            backlog_new_orders=int(metrics["backlog_new_orders"]),
            fallback_count_in_window=len(self._fallback_event_times),
            hard_failure_count_in_window=len(self._hard_failure_event_times),
            route_drift_ratio=float(metrics["route_drift_ratio"]),
        )

    def _build_decision_planner_snapshot(
        self,
        runtime_state: RuntimeStateView,
    ) -> DecisionPlannerSnapshot:
        metrics = self._compute_planner_snapshot_metrics(runtime_state)
        self._prune_window_events(self._fallback_event_times, runtime_state.t_now)
        self._prune_window_events(self._hard_failure_event_times, runtime_state.t_now)
        return DecisionPlannerSnapshot(
            backlog_new_orders=int(metrics["backlog_new_orders"]),
            fallback_count_in_window=len(self._fallback_event_times),
            hard_failure_count_in_window=len(self._hard_failure_event_times),
            route_drift_ratio=float(metrics["route_drift_ratio"]),
            completed_backbone_count=int(metrics["completed_backbone_count"]),
            expected_backbone_count=int(metrics["expected_backbone_count"]),
            total_backbone_count=int(metrics["total_backbone_count"]),
            active_launch_stations=tuple(sorted(self._active_launch_stations)),
        )

    def _build_decision_execution_snapshot(
        self,
        runtime_state: RuntimeStateView,
    ) -> DecisionExecutionSnapshot:
        return DecisionExecutionSnapshot(
            uav_eta_to_available={
                drone_id: float(
                    self._estimate_eta_to_available_for_snapshot(
                        drone_id=drone_id,
                        t_now=float(runtime_state.t_now),
                    )
                )
                for drone_id in runtime_state.drone_states
            },
            uav_dispatch_mode={
                drone_id: (
                    self._dispatch_commit[drone_id].mode.value
                    if drone_id in self._dispatch_commit
                    else "NONE"
                )
                for drone_id in runtime_state.drone_states
            },
        )

    def _compute_planner_snapshot_metrics(
        self,
        runtime_state: RuntimeStateView,
    ) -> dict[str, float | int]:
        backlog_new_orders = 0
        route_drift_ratio = 0.0
        completed_count = len(self._completed_backbone_nodes_since_plan)
        expected_count = 0
        total_backbone_count = 0
        if self._current_coarse_plan is not None:
            backlog_new_orders = sum(
                1
                for order_id, order in runtime_state.pending_orders.items()
                if self._is_uav_primary_order(order)
                and not self._current_coarse_plan.is_order_authorized(order_id)
            )
            total_backbone_count = len(self._current_coarse_plan.route_drift_ref)
            if total_backbone_count > 0:
                expected_count = sum(
                    1
                    for item in self._current_coarse_plan.route_drift_ref.values()
                    if float(item.eta_ref) <= float(runtime_state.t_now) + _TIME_EPS
                )
                route_drift_ratio = abs(completed_count - expected_count) / total_backbone_count
        return {
            "backlog_new_orders": int(backlog_new_orders),
            "route_drift_ratio": float(route_drift_ratio),
            "completed_backbone_count": int(completed_count),
            "expected_backbone_count": int(expected_count),
            "total_backbone_count": int(total_backbone_count),
        }

    def _estimate_eta_to_available_for_snapshot(
        self,
        *,
        drone_id: str,
        t_now: float,
    ) -> float:
        state = self._drone_state.get(drone_id)
        if state is None:
            return 0.0
        if state in {
            TrainingDroneState.IDLE,
            TrainingDroneState.RIDING_WITH_TRUCK,
            TrainingDroneState.AIRBORNE_ENERGY_FAILURE,
        }:
            return 0.0
        if state == TrainingDroneState.DELIVERY_SERVICE:
            service_leg = self._delivery_service_legs.get(drone_id)
            if service_leg is not None:
                return max(0.0, float(service_leg.finish_time) - t_now)
        if drone_id in self._flight_legs:
            return max(0.0, float(self._flight_legs[drone_id].arrival_time) - t_now)
        if state == TrainingDroneState.WAITING_FOR_TRUCK:
            commit = self._dispatch_commit.get(drone_id)
            if commit is not None and commit.selected_recover_node is not None:
                arrival = self._future_arrival_time_for_node(commit.selected_recover_node, t_now)
                if arrival is not None:
                    return max(0.0, arrival - t_now)
        if state == TrainingDroneState.CHARGING_ON_TRUCK:
            done_time = self._truck_charge_until.get(drone_id)
            if done_time is not None:
                return max(0.0, float(done_time) - t_now)
        return 0.0

    def _future_arrival_time_for_node(
        self,
        node_id: str,
        t_now: float,
    ) -> float | None:
        future_visits = self._future_backbone_visits(t_now)
        for visit in future_visits:
            if visit.node_id == node_id:
                return float(visit.arrival_time)
        return None

    def _prune_window_events(self, events: list[float], t_now: float) -> None:
        window_start = t_now - self._cfg.fallback_burst_window_sec
        while events and events[0] < window_start - _TIME_EPS:
            events.pop(0)

    def _prune_active_launch_stations(self, runtime_state: RuntimeStateView) -> None:
        if not self._active_launch_stations:
            return
        removable = [
            node_id
            for node_id in self._active_launch_stations
            if not self._station_has_pending_orders(runtime_state, node_id)
        ]
        for node_id in removable:
            self._active_launch_stations.discard(node_id)

    def _station_has_pending_orders(
        self,
        runtime_state: RuntimeStateView,
        station_id: str,
    ) -> bool:
        node_state = runtime_state.node_states.get(station_id)
        if node_state is None:
            return False
        radius_m = self._cfg.support_radius_km * 1000.0
        for order in runtime_state.pending_orders.values():
            if not self._is_uav_primary_order(order):
                continue
            if node_state.position.distance_2d(order.delivery_loc) <= radius_m + _TIME_EPS:
                return True
        return False

    def _build_action_lookup(
        self,
        *,
        drone_id: str,
        coarse_plan: CoarsePlanView,
        runtime_state: RuntimeStateView | None = None,
        trigger_type: str | None = None,
        trigger_station_id: str | None = None,
    ) -> tuple[EnvAction, ...]:
        """为当前决策 UAV 构造可执行动作列表。"""
        if runtime_state is None:
            runtime_state = self.build_runtime_state_view()
        effective_trigger_type = (
            trigger_type
            if trigger_type is not None
            else (
                "truck_station_arrival"
                if self._drone_state.get(drone_id) == TrainingDroneState.RIDING_WITH_TRUCK
                else "inline_idle"
            )
        )
        candidate_out = self._candidate_builder.build(
            runtime_state=runtime_state,
            coarse_plan=coarse_plan,
            deciding_drone_id=drone_id,
            trigger_type=effective_trigger_type,
            trigger_station_id=trigger_station_id,
            last_seen_plan_version=coarse_plan.plan_version,
        )
        return candidate_out.resolved_action_lookup.as_action_lookup()

    # ---------------------------------------------------------------------
    # Step helpers
    # ---------------------------------------------------------------------

    def _apply_wait_action(self, drone_id: str) -> None:
        """把当前 UAV 切入 `active_wait` 占位状态。"""
        current_state = self._drone_state[drone_id]
        if current_state == TrainingDroneState.IDLE:
            self._active_wait_resume[drone_id] = TrainingDroneState.IDLE
        elif current_state == TrainingDroneState.RIDING_WITH_TRUCK:
            self._active_wait_resume[drone_id] = TrainingDroneState.RIDING_WITH_TRUCK
        else:
            raise RuntimeError(f"状态 {current_state.value} 不允许执行 WAIT")
        # idle 路径在 step(WAIT) 入口一次性结算；riding_with_truck 路径后续按真实 dt 累计。
        self._drone_state[drone_id] = TrainingDroneState.ACTIVE_WAIT

    def _apply_dispatch_action(
        self,
        trigger: DecisionTrigger,
        action: DispatchAction,
    ) -> None:
        """把 dispatch 动作落到订单池、无人机绑定和飞行账本上。"""
        entity_mgr = self._require_entity_manager()
        order_mgr = self._require_order_manager()
        drone = entity_mgr.drones[trigger.drone_id]
        order = order_mgr.pending_orders.pop(action.order_id)

        order.assigned_vehicle_id = drone.drone_id
        order.assigned_mode = action.mode.value
        order.update_status(TaskStatus.ASSIGNED)
        order.update_status(TaskStatus.PICKED_UP)
        order.update_status(TaskStatus.DELIVERING)
        order_mgr.assigned_orders[order.order_id] = order

        drone.assign_order(order.order_id, float(order.payload_weight))
        if self._drone_state[drone.drone_id] == TrainingDroneState.RIDING_WITH_TRUCK:
            if drone.drone_id in self._require_truck().docked_drones:
                self._require_truck().docked_drones.remove(drone.drone_id)

        self._dispatch_commit[drone.drone_id] = DispatchCommit(
            order_id=order.order_id,
            mode=action.mode,
            selected_recover_node=action.recover_node_id,
            trigger_station_id=trigger.trigger_station_id,
        )
        if action.mode == PolicyMode.C and action.recover_node_id is not None:
            self._acquire_reservation(
                drone_id=drone.drone_id,
                order=order,
                recover_node_id=action.recover_node_id,
            )
        launch_time = self._resolve_dispatch_launch_time(
            drone_id=drone.drone_id,
            trigger=trigger,
        )
        self._schedule_flight_leg(
            drone_id=drone.drone_id,
            kind="deliver",
            target_pos=_clone_position(order.delivery_loc),
            payload=float(order.payload_weight),
            target_order_id=order.order_id,
            start_time=launch_time,
        )
        self._drone_state[drone.drone_id] = TrainingDroneState.FLYING_TO_DELIVER

    def _build_decision_context(self, trigger: DecisionTrigger) -> DecisionContext:
        """把内部 trigger 展开成给调用方可直接消费的决策上下文。"""
        runtime_state = self.build_runtime_state_view()
        coarse_plan = self._refresh_coarse_plan_if_needed(runtime_state)
        planner_snapshot = self._build_decision_planner_snapshot(runtime_state)
        execution_snapshot = self._build_decision_execution_snapshot(runtime_state)
        action_lookup = self._build_action_lookup(
            drone_id=trigger.drone_id,
            coarse_plan=coarse_plan,
            runtime_state=runtime_state,
            trigger_type=trigger.trigger_type,
            trigger_station_id=trigger.trigger_station_id,
        )
        return DecisionContext(
            t_decision=float(runtime_state.t_now),
            deciding_drone_id=trigger.drone_id,
            trigger_type=trigger.trigger_type,
            trigger_station_id=trigger.trigger_station_id,
            runtime_state=runtime_state,
            coarse_plan=coarse_plan,
            planner_snapshot=planner_snapshot,
            execution_snapshot=execution_snapshot,
            action_lookup=action_lookup,
        )

    def _build_step_result(
        self,
        *,
        reward: float,
        info: Mapping[str, Any],
    ) -> EnvStepResult:
        """把当前内部状态封装成一次统一返回结果。"""
        decision_context = self.current_decision_context
        runtime_state = self.build_runtime_state_view()
        merged_info = dict(info)
        if self._last_reward_breakdown:
            merged_info["reward_breakdown"] = dict(self._last_reward_breakdown)
        merged_info["system_context_stats"] = self.build_system_context_stats()
        return EnvStepResult(
            reward=float(reward),
            done=self.is_done(),
            runtime_state=runtime_state,
            decision_context=decision_context,
            info=merged_info,
        )

    # ---------------------------------------------------------------------
    # Event loop
    # ---------------------------------------------------------------------

    def _advance_until_decision_or_done(self) -> None:
        """持续推进事件，直到出现新的决策点或 episode 结束。"""
        while not self.is_done() and not self._decision_queue:
            next_time = self._next_event_time()
            if math.isinf(next_time):
                next_time = self._cfg.upper_horizon_sec
            if next_time <= self._t_now + _TIME_EPS:
                next_time = min(self._cfg.upper_horizon_sec, self._t_now + 1.0)
            self._advance_to_event(next_time)

    def _advance_to_event(self, t_next: float) -> float:
        """推进到给定绝对时刻，并结算该区间内跨过的事件。

        返回值为全局事件奖励（仅用于 _last_reward_breakdown 记录）。
        per-dt 成本和事件奖励均已写入 _agent_cost_accum，step() 从中按 drone 取值。
        """
        if t_next < self._t_now - _TIME_EPS:
            raise ValueError(f"不能回退时间: t_next={t_next}, t_now={self._t_now}")

        entity_mgr = self._require_entity_manager()
        truck = self._require_truck()
        order_mgr = self._require_order_manager()

        self._sync_in_transit_positions(t_next)

        # per-dt 成本已写入 _agent_cost_accum，global_reward 仅用于 breakdown 记录
        _global_per_dt, reward_breakdown = self._settle_per_dt_rewards(
            t_prev=self._t_now,
            t_next=t_next,
        )
        hard_failure_ready = self._collect_airborne_failure_events(t_next)
        failed_drones = {drone_id for drone_id, _, _ in hard_failure_ready}
        # delivery 先处理，保持"送达奖励优先于后续到达交互"的顺序。
        delivery_ready = [
            (drone_id, leg)
            for drone_id, leg in self._collect_flight_events(t_next, kind="deliver")
            if drone_id not in failed_drones
        ]
        non_delivery_ready = [
            (drone_id, leg)
            for drone_id, leg in self._collect_flight_events(
                t_next,
                kind=None,
                exclude={"deliver"},
            )
            if drone_id not in failed_drones
        ]
        delivery_service_ready = self._collect_delivery_service_events(t_next)
        truck_charge_ready = [
            drone_id
            for drone_id, done_time in list(self._truck_charge_until.items())
            if done_time <= t_next + _TIME_EPS
        ]
        truck_stops = self._collect_truck_stops(t_next)

        event_reward = 0.0
        delivery_reward = 0.0
        hard_failure_reward = 0.0
        hard_overdue_reward = 0.0
        reservation_timeout_reward = 0.0

        # 1. 硬失败事件：半空停电会在到达事件之前截断当前飞行段。
        for drone_id, _leg, _failure_time in hard_failure_ready:
            penalty = self._process_airborne_failure_event(drone_id)
            hard_failure_reward += penalty
            # 归因给失败的无人机
            self._agent_cost_accum[drone_id] = (
                self._agent_cost_accum.get(drone_id, 0.0) + penalty
            )

        # 2. UAV 订单送达 / mode A 背景完成（只记系统上下文统计，不进 PPO reward）
        for drone_id, leg in delivery_ready:
            bonus = self._process_delivery_event(drone_id, leg)
            delivery_reward += bonus
            # 送达奖励归因给完成送达的无人机
            self._agent_cost_accum[drone_id] = (
                self._agent_cost_accum.get(drone_id, 0.0) + bonus
            )
        for drone_id, service_leg in delivery_service_ready:
            self._process_delivery_service_event(drone_id, service_leg)
        for stop in truck_stops:
            if stop.node_type == "customer" and stop.order_id:
                if stop.order_id in self._background_mode_a_pending:
                    self._background_mode_a_pending.remove(stop.order_id)
                    self._background_mode_a_completed.add(stop.order_id)
                    self._background_mode_a_completion_time_sum += float(
                        stop.arrival_time
                    )
                    self._truck_background_order_completion_events.append(
                        {
                            "order_id": str(stop.order_id),
                            "stop_seq": int(stop.seq),
                            "node_id": str(stop.node_id),
                            "arrival_time_sec": float(stop.arrival_time),
                            "departure_time_sec": float(stop.departure_time),
                        }
                    )

        event_reward += delivery_reward + hard_failure_reward
        if delivery_reward:
            reward_breakdown["delivery_bonus"] = delivery_reward
        if hard_failure_reward:
            reward_breakdown["hard_failure"] = hard_failure_reward

        # 3a. 卡车到站
        station_arrivals = [stop for stop in truck_stops if stop.node_type == "station"]
        if truck_stops:
            last_stop = truck_stops[-1]
            truck.current_loc = _clone_position(last_stop.position)
        if self._current_coarse_plan is not None:
            for stop in station_arrivals:
                if stop.node_id in self._current_coarse_plan.route_drift_ref:
                    self._completed_backbone_nodes_since_plan.add(stop.node_id)

        # 3b. UAV 到站 + host charge complete + truck charge complete
        newly_idle_from_host: list[str] = []
        for drone_id, leg in non_delivery_ready:
            self._process_non_delivery_arrival(drone_id, leg)

        for drone_id in truck_charge_ready:
            self._truck_charge_until.pop(drone_id, None)
            drone = entity_mgr.drones[drone_id]
            drone.recharge_to_full()
            drone.current_loc = _clone_position(truck.current_loc)
            if drone_id not in truck.docked_drones:
                truck.docked_drones.append(drone_id)
            self._drone_state[drone_id] = TrainingDroneState.RIDING_WITH_TRUCK

        for host in list(entity_mgr.stations.values()) + list(entity_mgr.depots.values()):
            completed = host.tick_update(t_next)
            if completed:
                newly_idle_from_host.extend(completed)
            self._sync_host_service_states(host)

        for drone_id in newly_idle_from_host:
            drone = entity_mgr.drones[drone_id]
            drone.recharge_to_full()
            if drone_id in self._dispatch_commit:
                self._dispatch_commit.pop(drone_id, None)
            self._drone_state[drone_id] = TrainingDroneState.IDLE

        # 3c. 由到达触发的交互
        if truck_stops:
            for stop in truck_stops:
                self._process_rendezvous_recovery(stop.node_id, t_next)

        # 4. hard overdue + reservation timeout（已在各自函数内写入 _agent_cost_accum）
        hard_overdue_reward += self._apply_hard_overdue_penalty(t_next)
        reservation_timeout_reward += self._process_reservation_timeouts(t_next)
        event_reward += hard_overdue_reward + reservation_timeout_reward
        if hard_overdue_reward:
            reward_breakdown["hard_overdue"] = hard_overdue_reward
        if reservation_timeout_reward:
            reward_breakdown["reservation_timeout"] = reservation_timeout_reward

        # 5. poisson / benchmark 动态订单注入
        order_mgr.tick(t_next, entity_mgr)

        # 6. 生成 decision context / done
        self._t_now = min(float(t_next), float(self._cfg.upper_horizon_sec))
        station_trigger: str | None = None
        refreshed_runtime_state = self.build_runtime_state_view()
        self._refresh_coarse_plan_if_needed(refreshed_runtime_state)
        for stop in station_arrivals:
            if stop.node_id in self._active_launch_stations:
                station_trigger = stop.node_id
            self._active_launch_stations.discard(stop.node_id)
        if station_trigger is not None:
            # 车到站先恢复 riding_with_truck 的 WAIT 占位，再为当前仍在车上的 UAV 产生触发。
            self._resume_active_wait_for_station_trigger()
            for drone_id, state in self._drone_state.items():
                if state == TrainingDroneState.RIDING_WITH_TRUCK:
                    self._enqueue_decision(
                        drone_id,
                        trigger_type="truck_station_arrival",
                        trigger_station_id=station_trigger,
                    )
        if newly_idle_from_host:
            # 固定节点充换电完成会恢复 idle 上的 WAIT 占位，并为真正空闲的 UAV 重新建决策点。
            self._resume_active_wait_for_idle_trigger()
            for drone_id in newly_idle_from_host:
                if self._drone_state.get(drone_id) == TrainingDroneState.IDLE:
                    self._enqueue_decision(
                        drone_id,
                        trigger_type="idle_ready",
                        trigger_station_id=None,
                    )

        # order_mgr.tick() 可能注入了新 Poisson 订单；唤醒因无单而"睡死"的 idle UAV。
        self._wake_stranded_idle_drones()

        self._last_reward_breakdown = reward_breakdown
        # 返回全局总奖励（per-dt + 事件），供测试和 breakdown 观察。
        # step() 不使用此返回值，而是从 _agent_cost_accum 按 drone 取归因奖励。
        return _global_per_dt + event_reward

    # ---------------------------------------------------------------------
    # Event processing
    # ---------------------------------------------------------------------

    def _process_delivery_event(self, drone_id: str, leg: FlightLeg) -> float:
        """处理一次送达事件，并立即衔接送达后的执行层转移。"""
        entity_mgr = self._require_entity_manager()
        order_mgr = self._require_order_manager()
        drone = entity_mgr.drones[drone_id]
        commit = self._dispatch_commit[drone_id]

        drone.current_loc = _clone_position(leg.target_pos)
        drone.battery_current = max(0.0, drone.battery_current - leg.energy_cost_j)
        released_order_id = drone.release_order()
        if released_order_id != commit.order_id:
            raise RuntimeError("delivery_event 订单绑定不一致")

        order = order_mgr.assigned_orders.pop(commit.order_id)
        order.actual_deliver_time = float(leg.arrival_time)
        order.update_status(TaskStatus.COMPLETED)
        order_mgr.completed_orders.append(order)
        self._episode_delivery_count += 1
        self._flight_legs.pop(drone_id, None)
        self._drone_state[drone_id] = TrainingDroneState.DELIVERY_SERVICE

        service_duration = float(self._scene_solver_params().drone_service_time_order_s)
        if service_duration <= _TIME_EPS:
            self._process_delivery_service_event(
                drone_id,
                DeliveryServiceLeg(
                    order_id=commit.order_id,
                    start_time=float(leg.arrival_time),
                    finish_time=float(leg.arrival_time),
                    service_pos=_clone_position(leg.target_pos),
                ),
            )
            return self._cfg.R_delivery_bonus

        self._delivery_service_legs[drone_id] = DeliveryServiceLeg(
            order_id=commit.order_id,
            start_time=float(leg.arrival_time),
            finish_time=float(leg.arrival_time + service_duration),
            service_pos=_clone_position(leg.target_pos),
        )

        return self._cfg.R_delivery_bonus

    def _process_delivery_service_event(
        self,
        drone_id: str,
        service_leg: DeliveryServiceLeg,
    ) -> None:
        """处理客户点 delivery service 结束后的后续转移。"""
        self._delivery_service_legs.pop(drone_id, None)
        drone = self._require_entity_manager().drones[drone_id]
        commit = self._dispatch_commit[drone_id]
        drone.current_loc = _clone_position(service_leg.service_pos)
        self._drone_state[drone_id] = TrainingDroneState.DELIVERED

        if commit.mode == PolicyMode.B:
            target = self._select_return_host_mode_b(
                drone_id,
                current_time=service_leg.finish_time,
            )
            if target is None:
                self._mark_hard_failure(drone_id)
                return
            self._schedule_return_to_host(
                drone_id,
                target,
                start_time=service_leg.finish_time,
            )
            return

        selected_node = commit.selected_recover_node
        if selected_node:
            is_valid, _checks = self._revalidate_mode_c_recover_node(
                drone_id=drone_id,
                node_id=selected_node,
                t_now=service_leg.finish_time,
            )
        else:
            is_valid = False
        if is_valid:
            self._schedule_return_to_rendezvous(
                drone_id,
                selected_node,
                start_time=service_leg.finish_time,
            )
            return

        self._release_reservation(drone_id)
        if not self._enter_fallback_recovery(
            drone_id,
            start_time=service_leg.finish_time,
        ):
            self._mark_hard_failure(drone_id)

    def _process_non_delivery_arrival(self, drone_id: str, leg: FlightLeg) -> None:
        """处理除送达以外的飞行到达事件。"""
        entity_mgr = self._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        drone.current_loc = _clone_position(leg.target_pos)
        drone.battery_current = max(0.0, drone.battery_current - leg.energy_cost_j)
        self._flight_legs.pop(drone_id, None)

        if leg.kind == "return_to_rendezvous":
            self._drone_state[drone_id] = TrainingDroneState.WAITING_FOR_TRUCK
            return

        host = self._resolve_host(leg.target_node_id, leg.target_node_type)
        if leg.kind == "fallback_recovery":
            # fallback 到达后直接进入统一 charging host 入口，不再转成 return_to_station/depot 二次飞行。
            self._fallback_leg.pop(drone_id, None)
            self._on_arrive_charging_host(drone_id, host, leg.arrival_time)
            return

        if leg.kind in {"return_to_station", "return_to_depot"}:
            self._on_arrive_charging_host(drone_id, host, leg.arrival_time)
            return

        raise RuntimeError(f"未知飞行段类型: {leg.kind}")

    def _process_rendezvous_recovery(self, node_id: str, t_now: float) -> None:
        """处理"卡车到达某个 rendezvous 节点"时的回收配对。"""
        truck = self._require_truck()
        recovered: list[str] = []
        for drone_id, state in self._drone_state.items():
            if state != TrainingDroneState.WAITING_FOR_TRUCK:
                continue
            commit = self._dispatch_commit.get(drone_id)
            if commit is None or commit.selected_recover_node != node_id:
                continue
            recovered.append(drone_id)

        for drone_id in recovered:
            self._release_reservation(drone_id)
            self._drone_state[drone_id] = TrainingDroneState.CHARGING_ON_TRUCK
            self._truck_charge_until[drone_id] = (
                t_now + self._scene_solver_params().truck_drone_recover_time_s
            )
            if drone_id not in truck.docked_drones:
                truck.docked_drones.append(drone_id)

    def _process_airborne_failure_event(
        self,
        drone_id: str,
    ) -> float:
        """处理一次真实的空中电量耗尽事件。"""
        drone = self._require_entity_manager().drones[drone_id]
        drone.battery_current = 0.0
        return self._mark_hard_failure(drone_id)

    # ---------------------------------------------------------------------
    # Scheduling helpers
    # ---------------------------------------------------------------------

    def _schedule_flight_leg(
        self,
        *,
        drone_id: str,
        kind: str,
        target_pos: Position3D,
        payload: float,
        start_time: float | None = None,
        target_order_id: str | None = None,
        target_node_id: str | None = None,
        target_node_type: str | None = None,
    ) -> None:
        """为 UAV 写入一段新的飞行账本。"""
        entity_mgr = self._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        effective_start_time = float(self._t_now if start_time is None else start_time)
        start_pos = _clone_position(drone.current_loc)
        energy_cost = self._estimate_energy_needed(
            drone=drone,
            from_pos=start_pos,
            to_pos=target_pos,
            payload=payload,
        )
        flight_time = self._estimate_flight_time(
            drone=drone,
            from_pos=start_pos,
            to_pos=target_pos,
        )
        self._flight_legs[drone_id] = FlightLeg(
            kind=kind,
            start_time=effective_start_time,
            arrival_time=float(effective_start_time + flight_time),
            start_pos=start_pos,
            target_pos=_clone_position(target_pos),
            order_id=target_order_id,
            target_node_id=target_node_id,
            target_node_type=target_node_type,
            energy_cost_j=energy_cost,
        )

    def _schedule_return_to_host(
        self,
        drone_id: str,
        host: ChargingHost,
        *,
        start_time: float | None = None,
    ) -> None:
        """为 mode B 或其他固定宿主返程写入飞行段。"""
        drone = self._require_entity_manager().drones[drone_id]
        effective_start_time = float(self._t_now if start_time is None else start_time)
        if isinstance(host, Depot):
            kind = "return_to_depot"
            self._drone_state[drone_id] = TrainingDroneState.RETURN_TO_DEPOT
            node_type = "depot"
            node_id = host.depot_id
        elif isinstance(host, SwapStation):
            kind = "return_to_station"
            self._drone_state[drone_id] = TrainingDroneState.RETURN_TO_STATION
            node_type = "station"
            node_id = host.station_id
        else:
            raise TypeError(f"不支持的 host 类型: {type(host)!r}")

        self._schedule_flight_leg(
            drone_id=drone_id,
            kind=kind,
            target_pos=_clone_position(host.get_location(effective_start_time)),
            payload=0.0,
            start_time=effective_start_time,
            target_node_id=node_id,
            target_node_type=node_type,
        )

    def _schedule_return_to_rendezvous(
        self,
        drone_id: str,
        node_id: str,
        *,
        start_time: float | None = None,
    ) -> None:
        """为 mode C 的 rendezvous 返程写入飞行段。"""
        host = self._resolve_fixed_node(node_id)
        effective_start_time = float(self._t_now if start_time is None else start_time)
        self._drone_state[drone_id] = TrainingDroneState.RETURN_TO_RENDEZVOUS
        self._schedule_flight_leg(
            drone_id=drone_id,
            kind="return_to_rendezvous",
            target_pos=_clone_position(host.get_location(effective_start_time)),
            payload=0.0,
            start_time=effective_start_time,
            target_node_id=node_id,
            target_node_type=_node_type_of_host(host),
        )

    def _enter_fallback_recovery(
        self,
        drone_id: str,
        *,
        start_time: float | None = None,
    ) -> bool:
        """让 UAV 进入 fallback_recovery，并建立对应账本。

        返回值表示是否找到了可落地的兜底宿主。
        """
        host = self._select_deterministic_fallback_host(drone_id)
        if host is None:
            return False

        effective_start_time = float(self._t_now if start_time is None else start_time)
        drone = self._require_entity_manager().drones[drone_id]
        target_pos = _clone_position(host.get_location(effective_start_time))
        arrival_time = effective_start_time + self._estimate_flight_time(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=target_pos,
        )
        self._fallback_leg[drone_id] = FallbackLeg(
            host_node_id=_host_id(host),
            host_node_type=_node_type_of_host(host),
            arrival_time=float(arrival_time),
        )
        self._episode_fallback_count += 1
        self._drone_state[drone_id] = TrainingDroneState.FALLBACK_RECOVERY
        self._fallback_event_times.append(effective_start_time)
        self._schedule_flight_leg(
            drone_id=drone_id,
            kind="fallback_recovery",
            target_pos=target_pos,
            payload=0.0,
            start_time=effective_start_time,
            target_node_id=_host_id(host),
            target_node_type=_node_type_of_host(host),
        )
        return True

    def _acquire_reservation(
        self,
        *,
        drone_id: str,
        order: Order,
        recover_node_id: str,
    ) -> None:
        """为一次 mode C dispatch 建立 reservation。"""
        if not self._cfg.reservation_enabled:
            return

        self._release_reservation(drone_id)
        drone = self._require_entity_manager().drones[drone_id]
        host = self._resolve_fixed_node(recover_node_id)
        deliver_fly_time = self._estimate_flight_time(
            drone=drone,
            from_pos=order.delivery_loc,
            to_pos=host.get_location(self._t_now),
        )
        tau_res = (
            self._cfg.reservation_alpha * deliver_fly_time
            + self._cfg.reservation_gamma * float(host.estimate_wait_time(self._t_now))
        )
        reservation = ReservationState(
            recover_node=recover_node_id,
            issued_at=float(self._t_now),
            expires_at=float(self._t_now + tau_res),
        )
        self._reservations[drone_id] = reservation
        self._reservation_count[recover_node_id] = self._reservation_count.get(recover_node_id, 0) + 1

    def _release_reservation(self, drone_id: str) -> None:
        """释放某架 UAV 当前 reservation，并回写节点计数。"""
        reservation = self._reservations.pop(drone_id, None)
        if reservation is None:
            return
        node_id = reservation.recover_node
        current = self._reservation_count.get(node_id, 0)
        if current <= 1:
            self._reservation_count.pop(node_id, None)
        else:
            self._reservation_count[node_id] = current - 1

    def _process_reservation_timeouts(self, t_now: float) -> float:
        """扫描所有 reservation timeout，并执行 5c 兜底转移。

        reservation timeout 是 fallback 路径的一部分，归因给持有该 reservation 的无人机。
        """
        reward = 0.0
        for drone_id, reservation in list(self._reservations.items()):
            timeout_cost = self._reservation_timeout_cost(drone_id, reservation, t_now)
            if timeout_cost is None:
                continue

            self._release_reservation(drone_id)
            commit = self._dispatch_commit.get(drone_id)
            if commit is not None:
                self._dispatch_commit[drone_id] = DispatchCommit(
                    order_id=commit.order_id,
                    mode=commit.mode,
                    selected_recover_node=None,
                    trigger_station_id=commit.trigger_station_id,
                )

            timeout_penalty = -self._cfg.lambda_res_timeout * timeout_cost
            self._episode_reservation_timeout_count += 1
            self._episode_reservation_timeout_cost_sec += float(timeout_cost)
            reward += timeout_penalty
            # 归因给持有该 reservation 的无人机
            self._agent_cost_accum[drone_id] = (
                self._agent_cost_accum.get(drone_id, 0.0) + timeout_penalty
            )
            if self._drone_state.get(drone_id) in {
                TrainingDroneState.RETURN_TO_RENDEZVOUS,
                TrainingDroneState.WAITING_FOR_TRUCK,
                TrainingDroneState.DELIVERED,
            }:
                if not self._enter_fallback_recovery(drone_id, start_time=t_now):
                    hard_penalty = self._mark_hard_failure(drone_id)
                    reward += hard_penalty
                    self._agent_cost_accum[drone_id] = (
                        self._agent_cost_accum.get(drone_id, 0.0) + hard_penalty
                    )
        return reward

    def _reservation_timeout_cost(
        self,
        drone_id: str,
        reservation: ReservationState,
        t_now: float,
    ) -> float | None:
        """返回 reservation timeout 的违规量；无 timeout 时返回 None。"""
        if self._drone_state.get(drone_id) not in {
            TrainingDroneState.DELIVERED,
            TrainingDroneState.RETURN_TO_RENDEZVOUS,
            TrainingDroneState.WAITING_FOR_TRUCK,
        }:
            return None

        drone = self._require_entity_manager().drones[drone_id]
        host = self._resolve_fixed_node(reservation.recover_node)
        host_pos = host.get_location(t_now)
        coarse_plan = self._build_coarse_plan_view(t_now)
        t_arrive_truck = coarse_plan.truck_eta_map.get(reservation.recover_node)
        effective_battery = self._effective_battery_current(drone_id, t_now)
        energy_feasible = self._can_reach_from(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=host_pos,
            payload=0.0,
            safe_margin=drone.safe_margin_j,
            battery_current=effective_battery,
        )
        t_arrive_uav = t_now + self._estimate_flight_time(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=host_pos,
        )

        if t_now >= reservation.expires_at - _TIME_EPS:
            return max(0.0, t_now - reservation.expires_at)
        if t_arrive_truck is None:
            return 0.0
        if (
            t_arrive_uav + self._cfg.rendezvous_eta_safe_margin_sec
            > t_arrive_truck + _TIME_EPS
        ):
            return (
                t_arrive_uav
                + self._cfg.rendezvous_eta_safe_margin_sec
                - t_arrive_truck
            )
        if not energy_feasible:
            return 0.0
        return None

    def _apply_hard_overdue_penalty(self, t_now: float) -> float:
        """强制移除超时过久仍未送达的订单，并将惩罚归因到对应无人机的累积器。"""
        reward = 0.0
        order_mgr = self._require_order_manager()

        pending_remove = [
            order_id
            for order_id, order in list(order_mgr.pending_orders.items())
            if self._is_hard_overdue_candidate(order, t_now)
        ]
        for order_id in pending_remove:
            order = order_mgr.pending_orders.pop(order_id)
            order.update_status(TaskStatus.TIMEOUT)
            order_mgr.completed_orders.append(order)
            self._episode_hard_overdue_count += 1
            # 未接单订单无 owner，惩罚仅进系统指标（不归因任何 drone）
            reward -= self._cfg.hard_overdue_penalty_sec

        assigned_remove = [
            order_id
            for order_id, order in list(order_mgr.assigned_orders.items())
            if self._is_hard_overdue_candidate(order, t_now)
        ]
        for order_id in assigned_remove:
            self._episode_hard_overdue_count += 1
            # 在移除前先记录 owner，以便归因
            owner = order_mgr.assigned_orders[order_id].assigned_vehicle_id
            penalty = self._force_remove_assigned_order(order_id, t_now)
            reward += penalty
            if owner and owner in self._drone_state:
                self._agent_cost_accum[owner] = (
                    self._agent_cost_accum.get(owner, 0.0) + penalty
                )

        return reward

    def _is_hard_overdue_candidate(self, order: Order, t_now: float) -> bool:
        """检查订单是否达到 hard overdue 强制移除阈值。"""
        if not self._is_uav_primary_order(order):
            return False
        return (t_now - float(order.deadline)) > self._cfg.max_overdue_sec + _TIME_EPS

    def _force_remove_assigned_order(self, order_id: str, t_now: float) -> float:
        """强制移除已指派但已 hard overdue 的订单。"""
        order_mgr = self._require_order_manager()
        order = order_mgr.assigned_orders.pop(order_id)
        order.update_status(TaskStatus.TIMEOUT)
        order_mgr.completed_orders.append(order)

        drone_id = order.assigned_vehicle_id
        if drone_id and drone_id in self._require_entity_manager().drones:
            drone = self._require_entity_manager().drones[drone_id]
            if drone.carrying_order_id == order_id:
                drone.release_order()
            self._flight_legs.pop(drone_id, None)
            self._delivery_service_legs.pop(drone_id, None)
            self._release_reservation(drone_id)
            self._dispatch_commit.pop(drone_id, None)
            if self._drone_state.get(drone_id) != TrainingDroneState.AIRBORNE_ENERGY_FAILURE:
                self._drone_state[drone_id] = TrainingDroneState.DELIVERED
                if not self._enter_fallback_recovery(drone_id, start_time=t_now):
                    return -self._cfg.hard_overdue_penalty_sec + self._mark_hard_failure(drone_id)

        return -self._cfg.hard_overdue_penalty_sec

    def _settle_per_dt_rewards(
        self,
        *,
        t_prev: float,
        t_next: float,
    ) -> tuple[float, dict[str, float]]:
        """结算 Phase 5c 的持续型奖励项，并将成本归因到各无人机的累积器。

        返回值仍保留 (global_reward, breakdown) 供 _advance_to_event 记录系统指标，
        但实际 PPO 奖励通过 _agent_cost_accum 按无人机归因，不再直接用 global_reward。
        """
        if t_next <= t_prev + _TIME_EPS:
            return 0.0, {}

        global_reward = 0.0
        breakdown: dict[str, float] = {}
        dt = t_next - t_prev

        # ── T_overdue：已接单订单归因给 owner drone；未接单订单仅进系统指标 ──
        assigned_overdue_dt = 0.0
        unassigned_overdue_dt = 0.0
        for order in self._active_uav_orders():
            overdue_dt = max(0.0, t_next - max(t_prev, float(order.deadline)))
            if overdue_dt <= _TIME_EPS:
                continue
            owner = order.assigned_vehicle_id
            if owner and owner in self._drone_state:
                # 已接单：归因给 owner drone
                penalty = -self._cfg.lambda_overdue * overdue_dt
                self._agent_cost_accum[owner] = (
                    self._agent_cost_accum.get(owner, 0.0) + penalty
                )
                assigned_overdue_dt += overdue_dt
            else:
                # 未接单：仅进系统指标，不污染任何 drone 的 PPO reward
                unassigned_overdue_dt += overdue_dt

        total_overdue_dt = assigned_overdue_dt + unassigned_overdue_dt
        self._episode_overdue_time_sec += total_overdue_dt
        if total_overdue_dt > _TIME_EPS:
            # breakdown 仍记录全局值，供 tensorboard 观察
            global_penalty = -self._cfg.lambda_overdue * total_overdue_dt
            global_reward += global_penalty
            breakdown["overdue"] = global_penalty
            if unassigned_overdue_dt > _TIME_EPS:
                breakdown["overdue_unassigned"] = (
                    -self._cfg.lambda_overdue * unassigned_overdue_dt
                )

        # ── T_wait / T_idle / T_queue / T_fallback：按状态归因给对应无人机 ──
        wait_dt_total = 0.0
        idle_wait_dt_total = 0.0
        queue_dt_total = 0.0
        fallback_dt_total = 0.0

        for drone_id, state in self._drone_state.items():
            if state == TrainingDroneState.WAITING_FOR_TRUCK:
                # T_wait：等待卡车的无人机自己承担
                penalty = -self._cfg.lambda_wait * dt
                self._agent_cost_accum[drone_id] = (
                    self._agent_cost_accum.get(drone_id, 0.0) + penalty
                )
                wait_dt_total += dt
            elif (
                state == TrainingDroneState.ACTIVE_WAIT
                and self._active_wait_resume.get(drone_id)
                == TrainingDroneState.RIDING_WITH_TRUCK
            ):
                # T_idle（riding_with_truck 路径）：显式 WAIT 的无人机自己承担
                penalty = -self._cfg.wait_idle_penalty_coef * dt
                self._agent_cost_accum[drone_id] = (
                    self._agent_cost_accum.get(drone_id, 0.0) + penalty
                )
                idle_wait_dt_total += dt
            elif state == TrainingDroneState.QUEUEING_AT_HOST:
                # T_queue：排队的无人机自己承担
                penalty = -self._cfg.lambda_queue * dt
                self._agent_cost_accum[drone_id] = (
                    self._agent_cost_accum.get(drone_id, 0.0) + penalty
                )
                queue_dt_total += dt
            elif state == TrainingDroneState.FALLBACK_RECOVERY:
                # T_fallback：fallback 的无人机自己承担
                penalty = -self._cfg.lambda_miss * dt
                self._agent_cost_accum[drone_id] = (
                    self._agent_cost_accum.get(drone_id, 0.0) + penalty
                )
                fallback_dt_total += dt

        self._episode_wait_time_sec += wait_dt_total
        self._episode_idle_time_sec += idle_wait_dt_total
        self._episode_queue_time_sec += queue_dt_total
        self._episode_fallback_time_sec += fallback_dt_total

        if wait_dt_total > _TIME_EPS:
            penalty = -self._cfg.lambda_wait * wait_dt_total
            global_reward += penalty
            breakdown["wait"] = penalty
        if idle_wait_dt_total > _TIME_EPS:
            penalty = -self._cfg.wait_idle_penalty_coef * idle_wait_dt_total
            global_reward += penalty
            breakdown["idle"] = penalty
        if queue_dt_total > _TIME_EPS:
            penalty = -self._cfg.lambda_queue * queue_dt_total
            global_reward += penalty
            breakdown["queue"] = penalty
        if fallback_dt_total > _TIME_EPS:
            penalty = -self._cfg.lambda_miss * fallback_dt_total
            global_reward += penalty
            breakdown["fallback"] = penalty

        return global_reward, breakdown

    # ---------------------------------------------------------------------
    # Selection helpers
    # ---------------------------------------------------------------------

    def _deliver_leg_feasible(self, drone: Drone, order: Order) -> bool:
        """检查当前电量是否足以把订单送到客户点。"""
        energy_need = self._estimate_energy_needed(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=order.delivery_loc,
            payload=float(order.payload_weight),
        )
        return energy_need <= drone.battery_current + _TIME_EPS

    def _estimate_delivery_arrival_time(self, drone: Drone, order: Order) -> float:
        """估算当前时刻派送该订单的送达绝对时刻。"""
        return self._estimate_delivery_launch_time(drone.drone_id) + self._estimate_flight_time(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=order.delivery_loc,
        )

    def _estimate_delivery_finish_time(self, drone: Drone, order: Order) -> float:
        """估算当前时刻派送该订单后完成 customer service 的绝对时刻。"""
        return (
            self._estimate_delivery_arrival_time(drone, order)
            + float(self._scene_solver_params().drone_service_time_order_s)
        )

    def _estimate_energy_after_delivery(self, drone: Drone, order: Order) -> float:
        """估算无人机送达订单后的剩余电量。"""
        return drone.battery_current - self._estimate_energy_needed(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=order.delivery_loc,
            payload=float(order.payload_weight),
        )

    def _iter_feasible_mode_c_recovery_nodes(
        self,
        *,
        drone: Drone,
        order: Order,
        coarse_plan: CoarsePlanView,
    ) -> tuple[str, ...]:
        """返回当前订单在 5b 语义下合法的 mode C 回收节点。"""
        t_deliver_finish = self._estimate_delivery_finish_time(drone, order)
        energy_after_delivery = self._estimate_energy_after_delivery(drone, order)
        if energy_after_delivery <= 0.0:
            return ()

        feasible_nodes: list[str] = []
        for recover_node_id in coarse_plan.get_recovery_candidates(order.order_id):
            if self._is_mode_c_recovery_feasible(
                drone=drone,
                deliver_pos=order.delivery_loc,
                recover_node_id=recover_node_id,
                t_deliver_finish=t_deliver_finish,
                energy_after_delivery=energy_after_delivery,
                coarse_plan=coarse_plan,
            ):
                feasible_nodes.append(recover_node_id)
        return tuple(feasible_nodes)

    def _is_mode_c_recovery_feasible(
        self,
        *,
        drone: Drone,
        deliver_pos: Position3D,
        recover_node_id: str,
        t_deliver_finish: float,
        energy_after_delivery: float,
        coarse_plan: CoarsePlanView,
    ) -> bool:
        """按 5b 的时序与能量口径判定单个 mode C 回收点是否合法。"""
        t_arrive_truck = coarse_plan.truck_eta_map.get(recover_node_id)
        if t_arrive_truck is None or t_arrive_truck <= t_deliver_finish + _TIME_EPS:
            return False

        host = self._resolve_fixed_node(recover_node_id)
        recover_pos = host.get_location(t_deliver_finish)
        t_arrive_uav = t_deliver_finish + self._estimate_flight_time(
            drone=drone,
            from_pos=deliver_pos,
            to_pos=recover_pos,
        )
        if (
            t_arrive_uav + self._cfg.rendezvous_eta_safe_margin_sec
            > t_arrive_truck + _TIME_EPS
        ):
            return False

        return self._can_reach_from(
            drone=drone,
            from_pos=deliver_pos,
            to_pos=recover_pos,
            payload=0.0,
            safe_margin=drone.safe_margin_j,
            battery_current=energy_after_delivery,
        )

    def _has_mode_b_return_host(self, drone: Drone, order: Order) -> bool:
        """检查送达后是否至少存在一个可达的 mode B 返程宿主。"""
        energy_after_delivery = self._estimate_energy_after_delivery(drone, order)
        if energy_after_delivery <= 0:
            return False

        return (
            self._select_return_host_mode_b(
                drone.drone_id,
                from_pos=order.delivery_loc,
                battery_current=energy_after_delivery,
                current_time=self._estimate_delivery_finish_time(drone, order),
            )
            is not None
        )

    def _select_return_host_mode_b(
        self,
        drone_id: str,
        *,
        from_pos: Position3D | None = None,
        battery_current: float | None = None,
        current_time: float | None = None,
    ) -> ChargingHost | None:
        """按 5b 规则选择 mode B 的确定性返程宿主。"""
        entity_mgr = self._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        eval_pos = _clone_position(from_pos) if from_pos is not None else _clone_position(drone.current_loc)
        eval_battery = float(drone.battery_current if battery_current is None else battery_current)
        eval_time = float(self._t_now if current_time is None else current_time)

        scored_hosts: list[tuple[float, float, float, float, str, ChargingHost]] = []
        for host in self._all_return_hosts():
            host_pos = host.get_location(eval_time)
            if not self._can_reach_from(
                drone=drone,
                from_pos=eval_pos,
                to_pos=host_pos,
                payload=0.0,
                safe_margin=drone.safe_margin_j,
                battery_current=eval_battery,
            ):
                continue

            fly_time = self._estimate_flight_time(
                drone=drone,
                from_pos=eval_pos,
                to_pos=host_pos,
            )
            predicted_queue_time = float(host.estimate_wait_time(eval_time))
            service_time = float(host.swap_time)
            score = fly_time + predicted_queue_time + service_time
            scored_hosts.append(
                (
                    score,
                    fly_time,
                    predicted_queue_time,
                    service_time,
                    _host_id(host),
                    host,
                )
            )

        if not scored_hosts:
            return None

        scored_hosts.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3],
                item[4],
            )
        )
        return scored_hosts[0][5]

    def _revalidate_mode_c_recover_node(
        self,
        *,
        drone_id: str,
        node_id: str,
        t_now: float,
    ) -> tuple[bool, dict[str, bool]]:
        """对 mode C 原选回收点执行 5b 的送达后复核。"""
        drone = self._require_entity_manager().drones[drone_id]
        coarse_plan = self._build_coarse_plan_view(t_now)

        node_still_valid = (
            node_id in coarse_plan.truck_backbone_route
            and node_id in coarse_plan.truck_eta_map
        )
        if not node_still_valid:
            checks = {
                "energy_feasible": False,
                "rendezvous_time_feasible": False,
                "node_still_valid": False,
            }
            return False, checks

        host = self._resolve_fixed_node(node_id)
        host_pos = host.get_location(t_now)
        energy_feasible = self._can_reach_from(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=host_pos,
            payload=0.0,
            safe_margin=drone.safe_margin_j,
            battery_current=drone.battery_current,
        )
        t_arrive_uav = t_now + self._estimate_flight_time(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=host_pos,
        )
        rendezvous_time_feasible = (
            t_arrive_uav + self._cfg.rendezvous_eta_safe_margin_sec
            <= coarse_plan.truck_eta_map[node_id] + _TIME_EPS
        )
        checks = {
            "energy_feasible": energy_feasible,
            "rendezvous_time_feasible": rendezvous_time_feasible,
            "node_still_valid": node_still_valid,
        }
        return all(checks.values()), checks

    def _select_deterministic_fallback_host(self, drone_id: str) -> ChargingHost | None:
        """按当前实现的 deterministic fallback 规则选宿主。"""
        entity_mgr = self._require_entity_manager()
        drone = entity_mgr.drones[drone_id]

        depot = self._require_depot()
        depot_pos = depot.get_location(self._t_now)
        if self._can_reach_from(
            drone=drone,
            from_pos=drone.current_loc,
            to_pos=depot_pos,
            payload=0.0,
            safe_margin=drone.safe_margin_j,
            battery_current=drone.battery_current,
        ):
            return depot

        reachable_stations: list[tuple[float, SwapStation]] = []
        for station in entity_mgr.stations.values():
            station_pos = station.get_location(self._t_now)
            if self._can_reach_from(
                drone=drone,
                from_pos=drone.current_loc,
                to_pos=station_pos,
                payload=0.0,
                safe_margin=drone.safe_margin_j,
                battery_current=drone.battery_current,
            ):
                reachable_stations.append(
                    (drone.current_loc.distance_2d(station_pos), station)
                )
        if not reachable_stations:
            return None
        reachable_stations.sort(key=lambda item: (item[0], item[1].station_id))
        return reachable_stations[0][1]

    def _active_uav_orders(self) -> tuple[Order, ...]:
        """返回进入 PPO 主指标口径的所有未送达订单。"""
        order_mgr = self._require_order_manager()
        active = [
            order
            for order in list(order_mgr.pending_orders.values()) + list(order_mgr.assigned_orders.values())
            if self._is_uav_primary_order(order)
        ]
        active.sort(key=lambda item: item.order_id)
        return tuple(active)

    def _serialize_visual_order(self, order: Order) -> dict[str, Any]:
        return {
            "order_id": str(order.order_id),
            "status": str(order.status.value),
            "x": float(order.delivery_loc.x),
            "y": float(order.delivery_loc.y),
            "z": float(order.delivery_loc.z),
            "create_time": float(order.create_time),
            "deadline": float(order.deadline),
            "payload_weight": float(order.payload_weight),
            "assigned_mode": order.assigned_mode,
            "assigned_vehicle_id": order.assigned_vehicle_id,
            "actual_deliver_time": (
                None
                if order.actual_deliver_time is None
                else float(order.actual_deliver_time)
            ),
        }

    def _is_uav_primary_order(self, order: Order) -> bool:
        """判断订单是否属于 UAV 主指标统计范围。"""
        return float(order.payload_weight) <= self._heavy_payload_capacity + _TIME_EPS

    # ---------------------------------------------------------------------
    # Position / time helpers
    # ---------------------------------------------------------------------

    def _compute_wait_delta(self, drone_id: str) -> float:
        """按文档定义计算本次 WAIT 的精确推进量。"""
        state = self._drone_state[drone_id]
        if state == TrainingDroneState.IDLE:
            next_decision_t = self._next_global_decision_event_time()
            target_time = min(
                next_decision_t,
                self._t_now + self._cfg.max_wait_decision_gap_sec,
                self._cfg.upper_horizon_sec,
            )
            return max(_TIME_EPS, target_time - self._t_now)

        if state == TrainingDroneState.RIDING_WITH_TRUCK:
            next_station_t = self._next_active_launch_station_arrival_time_on_route()
            if not math.isfinite(next_station_t):
                return max(_TIME_EPS, self._cfg.upper_horizon_sec - self._t_now)
            return max(_TIME_EPS, min(next_station_t, self._cfg.upper_horizon_sec) - self._t_now)

        raise RuntimeError(f"状态 {state.value} 不允许执行 WAIT")

    def _next_global_decision_event_time(self) -> float:
        """返回下一个会触发 PPO 决策的全局事件时刻。"""
        candidates: list[float] = []
        coarse_plan = self._refresh_coarse_plan_if_needed()
        launch_stations = (
            self._active_launch_stations
            if self._planner_bridge is not None
            else set(coarse_plan.launch_candidate_stations)
        )

        for stop in self._planned_route_stops[self._planned_route_stop_i :]:
            if (
                stop.node_type == "station"
                and stop.node_id in launch_stations
                and stop.arrival_time > self._t_now + _TIME_EPS
            ):
                candidates.append(stop.arrival_time)

        for host in list(self._require_entity_manager().stations.values()) + list(self._require_entity_manager().depots.values()):
            candidates.extend(
                finish_time
                for finish_time in host.serving_drones.values()
                if finish_time > self._t_now + _TIME_EPS
            )

        if not candidates:
            return math.inf
        return min(candidates)

    def _next_station_arrival_time_on_route(self) -> float:
        """返回卡车未来下一个 station 停靠时刻。"""
        for stop in self._planned_route_stops[self._planned_route_stop_i :]:
            if stop.node_type == "station" and stop.arrival_time > self._t_now + _TIME_EPS:
                return stop.arrival_time
        return math.inf

    def _next_active_launch_station_arrival_time_on_route(self) -> float:
        """返回卡车未来下一个真实触发站的到达时刻。"""
        for stop in self._planned_route_stops[self._planned_route_stop_i :]:
            if (
                stop.node_type == "station"
                and stop.node_id in self._active_launch_stations
                and stop.arrival_time > self._t_now + _TIME_EPS
            ):
                return stop.arrival_time
        return math.inf

    def _resume_capped_wait_if_needed(self, drone_id: str) -> None:
        """idle WAIT 被 gap 上限截断且未被事件恢复时，手动恢复到新决策点。"""
        if self._drone_state.get(drone_id) != TrainingDroneState.ACTIVE_WAIT:
            return
        resume_state = self._active_wait_resume.get(drone_id)
        if resume_state != TrainingDroneState.IDLE:
            return

        self._active_wait_resume.pop(drone_id, None)
        self._drone_state[drone_id] = TrainingDroneState.IDLE
        self._enqueue_decision(drone_id, "wait_resume", None)

    def _effective_battery_current(self, drone_id: str, t_now: float) -> float:
        """返回给定时刻的有效剩余电量（含飞行中线性耗电近似）。"""
        drone = self._require_entity_manager().drones[drone_id]
        leg = self._flight_legs.get(drone_id)
        if leg is None:
            return float(drone.battery_current)
        if t_now <= leg.start_time + _TIME_EPS:
            return float(drone.battery_current)
        if t_now >= leg.arrival_time - _TIME_EPS:
            return max(0.0, float(drone.battery_current) - float(leg.energy_cost_j))

        ratio = (t_now - leg.start_time) / max(_TIME_EPS, leg.arrival_time - leg.start_time)
        return max(0.0, float(drone.battery_current) - float(leg.energy_cost_j) * ratio)

    def _sync_in_transit_positions(self, t_now: float) -> None:
        """把 truck / UAV 的物理位置同步到给定时刻。"""
        entity_mgr = self._require_entity_manager()
        truck = self._require_truck()
        truck.current_loc = self._truck_position_at_time(t_now)

        for drone_id, state in self._drone_state.items():
            drone = entity_mgr.drones[drone_id]
            if state in {
                TrainingDroneState.RIDING_WITH_TRUCK,
                TrainingDroneState.CHARGING_ON_TRUCK,
            }:
                # 这两个状态下无人机位置直接跟随卡车当前位置。
                drone.current_loc = _clone_position(truck.current_loc)
            elif state == TrainingDroneState.ACTIVE_WAIT:
                resume_state = self._active_wait_resume.get(drone_id)
                if resume_state == TrainingDroneState.RIDING_WITH_TRUCK:
                    drone.current_loc = _clone_position(truck.current_loc)

        for drone_id, leg in self._flight_legs.items():
            drone = entity_mgr.drones[drone_id]
            if t_now <= leg.start_time + _TIME_EPS:
                drone.current_loc = _clone_position(leg.start_pos)
                continue
            if t_now >= leg.arrival_time - _TIME_EPS:
                drone.current_loc = _clone_position(leg.target_pos)
                continue
            # 对仍在飞行中的 UAV 做线性插值，只用于当前实现的运行时位置可视化。
            ratio = (t_now - leg.start_time) / max(_TIME_EPS, leg.arrival_time - leg.start_time)
            drone.current_loc = leg.start_pos.interpolate(leg.target_pos, ratio)

    def _next_event_time(self) -> float:
        """从当前所有已知事件源里取下一个未来事件时刻。"""
        candidates = [self._cfg.upper_horizon_sec]

        if self._planned_route_stop_i < len(self._planned_route_stops):
            candidates.append(self._planned_route_stops[self._planned_route_stop_i].arrival_time)

        for leg in self._flight_legs.values():
            candidates.append(leg.arrival_time)

        for service_leg in self._delivery_service_legs.values():
            candidates.append(service_leg.finish_time)

        failure_time = self._next_airborne_failure_time()
        if math.isfinite(failure_time):
            candidates.append(failure_time)

        for done_time in self._truck_charge_until.values():
            candidates.append(done_time)

        for reservation in self._reservations.values():
            if reservation.expires_at > self._t_now + _TIME_EPS:
                candidates.append(reservation.expires_at)

        for host in list(self._require_entity_manager().stations.values()) + list(self._require_entity_manager().depots.values()):
            for finish_time in host.serving_drones.values():
                candidates.append(float(finish_time))

        order_mgr = self._require_order_manager()
        next_poisson = float(getattr(order_mgr, "_next_order_time", math.inf))
        if math.isfinite(next_poisson):
            candidates.append(next_poisson)

        scheduled_dynamic = list(getattr(order_mgr, "_scheduled_dynamic", []))
        idx = int(getattr(order_mgr, "_scheduled_dynamic_i", 0))
        if idx < len(scheduled_dynamic):
            candidates.append(float(scheduled_dynamic[idx]["spawn_sim_s"]))

        future = [value for value in candidates if value > self._t_now + _TIME_EPS]
        if not future:
            return math.inf
        return min(future)

    def _next_airborne_failure_time(self) -> float:
        """返回当前最早的真实空中电量耗尽时刻。"""
        failure_times = [
            failure_time
            for drone_id, leg in self._flight_legs.items()
            if (failure_time := self._compute_airborne_failure_time(drone_id, leg)) is not None
        ]
        if not failure_times:
            return math.inf
        return min(failure_times)

    def _collect_flight_events(
        self,
        t_now: float,
        *,
        kind: str | None,
        exclude: set[str] | None = None,
    ) -> list[tuple[str, FlightLeg]]:
        """收集在 `t_now` 之前已经到达的飞行段事件。"""
        events: list[tuple[str, FlightLeg]] = []
        for drone_id, leg in list(self._flight_legs.items()):
            if leg.arrival_time > t_now + _TIME_EPS:
                continue
            if kind is not None and leg.kind != kind:
                continue
            if exclude is not None and leg.kind in exclude:
                continue
            events.append((drone_id, leg))
        events.sort(key=lambda item: (item[1].arrival_time, item[0]))
        return events

    def _collect_airborne_failure_events(
        self,
        t_now: float,
    ) -> list[tuple[str, FlightLeg, float]]:
        """收集在 `t_now` 前已经发生的空中电量耗尽事件。"""
        events: list[tuple[str, FlightLeg, float]] = []
        for drone_id, leg in list(self._flight_legs.items()):
            failure_time = self._compute_airborne_failure_time(drone_id, leg)
            if failure_time is None or failure_time > t_now + _TIME_EPS:
                continue
            events.append((drone_id, leg, failure_time))
        events.sort(key=lambda item: (item[2], item[0]))
        return events

    def _collect_truck_stops(self, t_now: float) -> list[PlannedStop]:
        """收集卡车在 `t_now` 之前已经到站的停靠点事件。"""
        stops: list[PlannedStop] = []
        while self._planned_route_stop_i < len(self._planned_route_stops):
            stop = self._planned_route_stops[self._planned_route_stop_i]
            if stop.arrival_time > t_now + _TIME_EPS:
                break
            stops.append(stop)
            self._planned_route_stop_i += 1
        return stops

    def _collect_delivery_service_events(
        self,
        t_now: float,
    ) -> list[tuple[str, DeliveryServiceLeg]]:
        """收集在 `t_now` 前已完成的 delivery service 事件。"""
        events: list[tuple[str, DeliveryServiceLeg]] = []
        for drone_id, service_leg in list(self._delivery_service_legs.items()):
            if service_leg.finish_time > t_now + _TIME_EPS:
                continue
            events.append((drone_id, service_leg))
        events.sort(key=lambda item: (item[1].finish_time, item[0]))
        return events

    # ---------------------------------------------------------------------
    # Charging / queue helpers
    # ---------------------------------------------------------------------

    def _on_arrive_charging_host(
        self,
        drone_id: str,
        host: ChargingHost,
        t_now: float,
    ) -> None:
        """统一处理 UAV 到达 depot/station 后的入队或入服。"""
        drone = self._require_entity_manager().drones[drone_id]
        drone.current_loc = _clone_position(host.get_location(t_now))
        host.arrive(drone_id, t_now)
        # 状态分流完全以 host.arrive() 的真实队列结果为准，不手写第二套判定。
        if drone_id in host.serving_drones:
            self._drone_state[drone_id] = TrainingDroneState.CHARGING_OR_SWAP
        elif drone_id in host.wait_queue:
            self._drone_state[drone_id] = TrainingDroneState.QUEUEING_AT_HOST
        else:
            raise RuntimeError("charging host arrival state inconsistent")

    def _sync_host_service_states(self, host: ChargingHost) -> None:
        """把 host 内部队列真值回写到训练状态层。"""
        for drone_id in list(host.serving_drones):
            if self._drone_state.get(drone_id) in {
                TrainingDroneState.QUEUEING_AT_HOST,
                TrainingDroneState.CHARGING_OR_SWAP,
            }:
                self._drone_state[drone_id] = TrainingDroneState.CHARGING_OR_SWAP
        for drone_id in list(host.wait_queue):
            if self._drone_state.get(drone_id) in {
                TrainingDroneState.QUEUEING_AT_HOST,
                TrainingDroneState.CHARGING_OR_SWAP,
            }:
                self._drone_state[drone_id] = TrainingDroneState.QUEUEING_AT_HOST

    # ---------------------------------------------------------------------
    # Decision queue helpers
    # ---------------------------------------------------------------------

    def _wake_stranded_idle_drones(self) -> None:
        """为既不在决策队列也不在 WAIT 中的 idle UAV 补入决策点。

        idle UAV 在无授权订单时会被 _enqueue_decision 的早返回"睡死"——
        既无队列条目也无 WAIT 占位。order_mgr.tick() 注入新单后调用此方法
        可确保这些 UAV 被重新唤醒。
        """
        queued = {item.drone_id for item in self._decision_queue}
        for drone_id, state in self._drone_state.items():
            # ACTIVE_WAIT 的 UAV 已有 WAIT 占位，到期后会自行恢复，无需处理。
            if state == TrainingDroneState.IDLE and drone_id not in queued:
                self._enqueue_decision(drone_id, "order_arrival_wake", None)

    def _enqueue_initial_idle_decisions(self) -> None:
        """为 reset 后处于 idle 的 UAV 批量创建初始决策点。"""
        for drone_id, state in sorted(self._drone_state.items()):
            if state == TrainingDroneState.IDLE:
                self._enqueue_decision(drone_id, "initial_idle", None)

    def _enqueue_decision(
        self,
        drone_id: str,
        trigger_type: str,
        trigger_station_id: str | None,
    ) -> None:
        """向内部决策队列追加一个触发点。"""
        state = self._drone_state.get(drone_id)
        if state not in {TrainingDroneState.IDLE, TrainingDroneState.RIDING_WITH_TRUCK}:
            return
        runtime_state = self.build_runtime_state_view()
        if not self._has_authorized_orders_now(runtime_state):
            return
        if any(item.drone_id == drone_id for item in self._decision_queue):
            return
        self._decision_queue.append(
            DecisionTrigger(
                drone_id=drone_id,
                trigger_type=trigger_type,
                trigger_station_id=trigger_station_id,
            )
        )

    def _resume_active_wait_for_idle_trigger(self) -> None:
        """在"固定节点空闲触发"发生时恢复 idle 上的 WAIT 占位。"""
        resumable = [
            drone_id
            for drone_id, state in self._active_wait_resume.items()
            if state == TrainingDroneState.IDLE
        ]
        for drone_id in resumable:
            self._active_wait_resume.pop(drone_id, None)
            self._drone_state[drone_id] = TrainingDroneState.IDLE
            self._enqueue_decision(drone_id, "wait_resume", None)

    def _resume_active_wait_for_station_trigger(self) -> None:
        """在"卡车到站触发"发生时恢复 WAIT 占位。"""
        resumable = list(self._active_wait_resume.items())
        for drone_id, state in resumable:
            self._active_wait_resume.pop(drone_id, None)
            self._drone_state[drone_id] = state
            if state == TrainingDroneState.IDLE:
                self._enqueue_decision(drone_id, "wait_resume", None)

    def _has_authorized_orders_now(
        self,
        runtime_state: RuntimeStateView | None = None,
    ) -> bool:
        """当前时刻是否存在 coarse plan 授权订单。"""
        if runtime_state is None:
            runtime_state = self.build_runtime_state_view()

        coarse_plan = self._refresh_coarse_plan_if_needed(runtime_state)
        live_authorized = any(
            order_id in runtime_state.pending_orders
            for order_id in coarse_plan.authorized_orders
        )
        if live_authorized:
            return True

        if self._planner_bridge is not None:
            pending_uav_orders = any(
                self._is_uav_primary_order(order)
                for order in runtime_state.pending_orders.values()
            )
            if pending_uav_orders:
                forced_ctx = self._build_planner_trigger_context(runtime_state)
                forced_ctx = PlannerTriggerContext(
                    t_now=forced_ctx.t_now,
                    backlog_new_orders=max(
                        forced_ctx.backlog_new_orders,
                        self._cfg.coarse_new_order_trigger,
                    ),
                    fallback_count_in_window=forced_ctx.fallback_count_in_window,
                    hard_failure_count_in_window=forced_ctx.hard_failure_count_in_window,
                    route_drift_ratio=forced_ctx.route_drift_ratio,
                )
                coarse_plan = self._planner_bridge.maybe_replan(runtime_state, forced_ctx)
                if (
                    self._current_coarse_plan is None
                    or coarse_plan.plan_version >= self._current_coarse_plan.plan_version
                ):
                    if (
                        self._current_coarse_plan is None
                        or coarse_plan.plan_version > self._current_coarse_plan.plan_version
                    ):
                        self._completed_backbone_nodes_since_plan.clear()
                    self._current_coarse_plan = coarse_plan
                    self._active_launch_stations = set(coarse_plan.launch_candidate_stations)
                    self._prune_active_launch_stations(runtime_state)
                return any(
                    order_id in runtime_state.pending_orders
                    for order_id in coarse_plan.authorized_orders
                )

        return False

    # ---------------------------------------------------------------------
    # Phase 4 / reset helpers
    # ---------------------------------------------------------------------

    def _load_phase4_artifacts(self) -> dict[str, Any]:
        """读取 Phase 4 路线产物，并转成当前环境内部结构。"""
        route_dir = self._phase4_route_dir
        execution_payload = _load_json(route_dir / "truck_execution_route.json")
        planned_stops: list[PlannedStop] = []
        backbone_cache: list[BackboneVisit] = []
        mode_a_order_ids: list[str] = []

        for raw_stop in execution_payload["stops"]:
            stop = PlannedStop(
                seq=int(raw_stop["seq"]),
                node_type=str(raw_stop["node_type"]),
                node_id=str(raw_stop["node_id"]),
                position=Position3D(
                    x=float(raw_stop["x"]),
                    y=float(raw_stop["y"]),
                    z=float(raw_stop.get("z", 0.0)),
                ),
                order_id=None if raw_stop.get("order_id") is None else str(raw_stop["order_id"]),
                arrival_time=float(raw_stop["arrival_time_sec"]),
                departure_time=float(raw_stop["departure_time_sec"]),
            )
            planned_stops.append(stop)

            if stop.node_type in {"station", "depot"} and stop.seq > 0:
                backbone_cache.append(
                    BackboneVisit(
                        node_id=stop.node_id,
                        arrival_time=stop.arrival_time,
                        departure_time=stop.departure_time + _BACKBONE_DEPARTURE_EPS,
                    )
                )
            if stop.node_type == "customer" and stop.order_id:
                # truck_execution_route 里的 customer stop 对应 mode A 背景完成事件。
                mode_a_order_ids.append(stop.order_id)

        return {
            "planned_stops": planned_stops,
            "backbone_cache": backbone_cache,
            "mode_a_order_ids": tuple(mode_a_order_ids),
        }

    def _append_patrol_loop_if_needed(self) -> None:
        """在 poisson 模式下按当前实现追加巡站循环。"""
        if not self._planned_route_stops:
            return
        if self._order_source.mode != OrderSourceMode.POISSON:
            return

        last_stop = self._planned_route_stops[-1]
        if last_stop.node_type != "depot":
            return
        if last_stop.arrival_time >= (
            self._cfg.upper_horizon_sec - self._cfg.patrol_min_remaining_sec
        ):
            return

        entity_mgr = self._require_entity_manager()
        truck = self._require_truck()
        depot = self._require_depot()
        stations = sorted(entity_mgr.stations.values(), key=lambda item: item.station_id)
        if not stations:
            return

        seq = self._planned_route_stops[-1].seq
        t_cursor = last_stop.departure_time
        patrol_k = min(len(stations), self._cfg.patrol_stations_per_loop)
        station_hold_time = max(
            float(self._scene_solver_params().truck_drone_launch_time_s),
            float(self._scene_solver_params().truck_drone_recover_time_s),
        )

        # 一旦决定进入 poisson 巡站补全路径，就持续追加到 upper horizon，
        # 避免 episode 尾段再次出现"未来 backbone 耗尽"的契约破口。
        while t_cursor < self._cfg.upper_horizon_sec:
            current_pos = _clone_position(depot.location)
            available = list(stations)
            chosen: list[SwapStation] = []

            for _ in range(patrol_k):
                # 当前实现按文档要求使用最近邻直线距离拼接巡站，不做 OSM 精确规划。
                next_station = min(
                    available,
                    key=lambda station: (
                        current_pos.distance_2d(station.location),
                        station.station_id,
                    ),
                )
                available.remove(next_station)
                chosen.append(next_station)
                current_pos = next_station.location

            current_pos = _clone_position(depot.location)
            for station in chosen:
                travel_time = current_pos.distance_2d(station.location) / max(_TIME_EPS, truck.speed)
                t_cursor += travel_time
                seq += 1
                departure_time = t_cursor + station_hold_time
                planned_stop = PlannedStop(
                    seq=seq,
                    node_type="station",
                    node_id=station.station_id,
                    position=_clone_position(station.location),
                    order_id=None,
                    arrival_time=t_cursor,
                    departure_time=departure_time,
                )
                self._planned_route_stops.append(planned_stop)
                self._full_backbone_cache.append(
                    BackboneVisit(
                        node_id=station.station_id,
                        arrival_time=t_cursor,
                        departure_time=departure_time + _BACKBONE_DEPARTURE_EPS,
                    )
                )
                current_pos = station.location
                t_cursor = departure_time

            travel_time_back = current_pos.distance_2d(depot.location) / max(_TIME_EPS, truck.speed)
            t_cursor += travel_time_back
            seq += 1
            # 巡站循环的 depot stop 只进入物理路线，不写入骨架缓存。
            self._planned_route_stops.append(
                PlannedStop(
                    seq=seq,
                    node_type="depot",
                    node_id=depot.depot_id,
                    position=_clone_position(depot.location),
                    order_id=None,
                    arrival_time=t_cursor,
                    departure_time=t_cursor,
                )
            )

        self._full_backbone_cache.sort(key=lambda item: (item.arrival_time, item.node_id))

    def _bind_truck_route(self) -> None:
        """把当前 `_planned_route_stops` 绑定到 Truck 实体的路线模型上。"""
        truck = self._require_truck()
        route_nodes = [stop.node_id for stop in self._planned_route_stops]
        route_positions = [_clone_position(stop.position) for stop in self._planned_route_stops]
        truck.set_route(
            route_nodes=route_nodes,
            route_positions=route_positions,
            departure_time=0.0,
        )

    def _initialize_drone_states(self) -> None:
        """根据 drone.home_type 建立训练侧初始状态。"""
        entity_mgr = self._require_entity_manager()
        truck = self._require_truck()
        for drone_id, drone in entity_mgr.drones.items():
            if drone.home_type == SourceType.TRUCK:
                self._drone_state[drone_id] = TrainingDroneState.RIDING_WITH_TRUCK
                drone.current_loc = _clone_position(truck.current_loc)
            else:
                self._drone_state[drone_id] = TrainingDroneState.IDLE
                drone.current_loc = _clone_position(self._require_depot().location)

    # ---------------------------------------------------------------------
    # Utilities
    # ---------------------------------------------------------------------

    def _mark_hard_failure(
        self,
        drone_id: str,
    ) -> float:
        """把 UAV 标记为硬失败，并返回一次性惩罚。"""
        drone = self._require_entity_manager().drones[drone_id]
        truck = self._require_truck()

        self._flight_legs.pop(drone_id, None)
        self._delivery_service_legs.pop(drone_id, None)
        self._fallback_leg.pop(drone_id, None)
        self._truck_charge_until.pop(drone_id, None)
        self._active_wait_resume.pop(drone_id, None)
        self._release_reservation(drone_id)
        self._decision_queue = [
            item for item in self._decision_queue if item.drone_id != drone_id
        ]
        if drone_id in truck.docked_drones:
            truck.docked_drones.remove(drone_id)
        drone.battery_current = 0.0
        self._drone_state[drone_id] = TrainingDroneState.AIRBORNE_ENERGY_FAILURE
        self._hard_failure_event_times.append(float(self._t_now))
        self._episode_hard_failure_count += 1
        return -self._cfg.hard_failure_penalty_sec

    def _episode_done_reason(self) -> str | None:
        if self._t_now >= self._cfg.upper_horizon_sec - _TIME_EPS:
            return "upper_horizon_reached"
        if (
            self._order_manager is not None
            and not self._order_manager.pending_orders
            and not self._order_manager.assigned_orders
            and not self._background_mode_a_pending
        ):
            return "all_orders_cleared"
        if self._drone_state and all(
            state == TrainingDroneState.AIRBORNE_ENERGY_FAILURE
            for state in self._drone_state.values()
        ):
            return "all_drones_hard_failed"
        return None

    def _compute_airborne_failure_time(
        self,
        drone_id: str,
        leg: FlightLeg,
    ) -> float | None:
        """按当前飞行账本估算"在到达前耗尽电量"的最早时刻。"""
        drone = self._require_entity_manager().drones[drone_id]
        if leg.energy_cost_j <= drone.battery_current + _TIME_EPS:
            return None
        if leg.arrival_time <= leg.start_time + _TIME_EPS:
            return self._t_now + _TIME_EPS

        failure_ratio = max(0.0, min(1.0, drone.battery_current / leg.energy_cost_j))
        failure_time = leg.start_time + (leg.arrival_time - leg.start_time) * failure_ratio
        # 若外部测试/调试在飞行中途手动改了电量，failure_time 可能已经落在当前时刻之前；
        # 此时把失败事件钳到"当前推进区间的下一个瞬间"。
        return max(self._t_now + _TIME_EPS, failure_time)

    def _estimate_flight_time(
        self,
        *,
        drone: Drone,
        from_pos: Position3D,
        to_pos: Position3D,
    ) -> float:
        """按三维直线距离和巡航速度估算飞行时长。"""
        return from_pos.distance_3d(to_pos) / max(_TIME_EPS, drone.cruise_speed)

    def _estimate_energy_needed(
        self,
        *,
        drone: Drone,
        from_pos: Position3D,
        to_pos: Position3D,
        payload: float,
    ) -> float:
        """按当前 Drone 功耗模型估算一段飞行能耗。"""
        distance = from_pos.distance_3d(to_pos)
        if distance <= _TIME_EPS:
            return 0.0
        power = drone.calculate_power(payload, drone.cruise_speed)
        return power * (distance / max(_TIME_EPS, drone.cruise_speed))

    def _can_reach_from(
        self,
        *,
        drone: Drone,
        from_pos: Position3D,
        to_pos: Position3D,
        payload: float,
        safe_margin: float,
        battery_current: float,
    ) -> bool:
        """在给定起点和剩余电量假设下判断 UAV 是否可达。"""
        return battery_current + _TIME_EPS >= (
            self._estimate_energy_needed(
                drone=drone,
                from_pos=from_pos,
                to_pos=to_pos,
                payload=payload,
            )
            + safe_margin
        )

    def _all_return_hosts(self) -> tuple[ChargingHost, ...]:
        """返回当前实现里所有可能的固定返程宿主。"""
        entity_mgr = self._require_entity_manager()
        return tuple(entity_mgr.depots.values()) + tuple(entity_mgr.stations.values())

    def _resolve_host(self, node_id: str | None, node_type: str | None) -> ChargingHost:
        """按 `(node_id, node_type)` 解析固定宿主对象。"""
        if not node_id or not node_type:
            raise ValueError("host 节点信息不完整")
        entity_mgr = self._require_entity_manager()
        if node_type == "depot":
            return entity_mgr.depots[node_id]
        if node_type == "station":
            return entity_mgr.stations[node_id]
        raise ValueError(f"不支持的 host node_type: {node_type}")

    def _resolve_fixed_node(self, node_id: str) -> ChargingHost:
        """按固定节点 ID 解析 station/depot。"""
        entity_mgr = self._require_entity_manager()
        if node_id in entity_mgr.stations:
            return entity_mgr.stations[node_id]
        if node_id in entity_mgr.depots:
            return entity_mgr.depots[node_id]
        raise KeyError(f"未知固定节点: {node_id}")

    def _scene_solver_params(self):
        """按需读取共享 solver 参数。"""
        from config.loader import load_solver_energy_params

        return load_solver_energy_params()

    def _resolve_dispatch_launch_time(
        self,
        *,
        drone_id: str,
        trigger: DecisionTrigger,
    ) -> float:
        """返回本次 dispatch 的真实起飞时刻。"""
        if (
            trigger.trigger_type == "truck_station_arrival"
            and trigger.trigger_station_id
            and self._drone_state.get(drone_id) == TrainingDroneState.RIDING_WITH_TRUCK
        ):
            return (
                self._t_now
                + float(self._scene_solver_params().truck_drone_launch_time_s)
            )
        return float(self._t_now)

    def _estimate_delivery_launch_time(self, drone_id: str) -> float:
        """估算当前状态下若立即 dispatch，其真实起飞时刻。"""
        if self._drone_state.get(drone_id) == TrainingDroneState.RIDING_WITH_TRUCK:
            return (
                self._t_now
                + float(self._scene_solver_params().truck_drone_launch_time_s)
            )
        return float(self._t_now)

    def _truck_position_at_time(self, t_now: float) -> Position3D:
        """按 planned stop 的 arrival/departure 窗口推演 truck 位置。"""
        if not self._planned_route_stops:
            return _clone_position(self._require_truck().current_loc)

        first_stop = self._planned_route_stops[0]
        if t_now <= first_stop.departure_time + _TIME_EPS:
            return _clone_position(first_stop.position)

        prev_stop = first_stop
        for stop in self._planned_route_stops[1:]:
            if t_now < stop.arrival_time - _TIME_EPS:
                segment_dt = max(_TIME_EPS, stop.arrival_time - prev_stop.departure_time)
                ratio = max(
                    0.0,
                    min(1.0, (t_now - prev_stop.departure_time) / segment_dt),
                )
                return prev_stop.position.interpolate(stop.position, ratio)
            if t_now <= stop.departure_time + _TIME_EPS:
                return _clone_position(stop.position)
            prev_stop = stop

        return _clone_position(self._planned_route_stops[-1].position)

    def _resolve_heavy_payload_capacity(self) -> float:
        """读取 heavy drone 的载重上限，供 coarse plan 做 mode A 边界判断。"""
        from config.loader import load_drone_params

        return float(load_drone_params().heavy.payload_capacity)

    def _is_action_allowed(
        self,
        action: EnvAction,
        action_lookup: tuple[EnvAction, ...],
    ) -> bool:
        """检查给定动作是否出现在当前候选动作列表中。"""
        return action in action_lookup

    def _require_entity_manager(self) -> EntityManager:
        """确保 entity_manager 已在 reset() 后初始化。"""
        if self._entity_manager is None:
            raise RuntimeError("reset() 前不能访问 entity_manager")
        return self._entity_manager

    def _require_order_manager(self) -> OrderManager:
        """确保 order_manager 已在 reset() 后初始化。"""
        if self._order_manager is None:
            raise RuntimeError("reset() 前不能访问 order_manager")
        return self._order_manager

    def _require_truck(self) -> Truck:
        """确保 truck 已在 reset() 后解析完成。"""
        if self._truck is None:
            raise RuntimeError("reset() 前不能访问 truck")
        return self._truck

    def _require_depot(self) -> Depot:
        """确保 depot 已在 reset() 后解析完成。"""
        if self._depot is None:
            raise RuntimeError("reset() 前不能访问 depot")
        return self._depot


def _load_env_yaml(config_path: Path) -> _YamlConfig:
    """读取训练 YAML，并抽取 env_adapter 当前实际使用的字段。"""
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("缺少 PyYAML，无法读取训练配置") from exc

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, Mapping):
        raise ValueError(f"YAML 顶层必须为 mapping: {config_path}")

    planner = _require_mapping(raw, "planner")
    reward = _require_mapping(raw, "reward")
    candidate = _require_mapping(raw, "candidate")
    reservation = _require_mapping(raw, "reservation")

    return _YamlConfig(
        upper_horizon_sec=float(planner["upper_horizon_sec"]),
        patrol_min_remaining_sec=float(planner["patrol_min_remaining_sec"]),
        patrol_stations_per_loop=int(planner["patrol_stations_per_loop"]),
        allow_empty_backbone_route=bool(planner.get("allow_empty_backbone_route", False)),
        support_radius_km=float(planner["support_radius_km"]),
        fallback_burst_window_sec=float(planner["fallback_burst_window_sec"]),
        coarse_new_order_trigger=int(planner["coarse_new_order_trigger"]),
        max_wait_decision_gap_sec=float(planner["max_wait_decision_gap_sec"]),
        max_candidate_recovery_per_order=int(candidate["max_candidate_recovery_per_order"]),
        rendezvous_eta_safe_margin_sec=float(candidate["rendezvous_eta_safe_margin_sec"]),
        reservation_enabled=bool(reservation.get("enable", True)),
        reservation_alpha=float(reservation["alpha"]),
        reservation_beta=float(reservation["beta"]),
        reservation_gamma=float(reservation["gamma"]),
        lambda_wait=float(reward["lambda_wait"]),
        wait_idle_penalty_coef=float(reward["wait_idle_penalty_coef"]),
        lambda_queue=float(reward["lambda_queue"]),
        lambda_miss=float(reward["lambda_miss"]),
        lambda_res_timeout=float(reward["lambda_res_timeout"]),
        lambda_overdue=float(reward["lambda_overdue"]),
        R_delivery_bonus=float(reward["R_delivery_bonus"]),
        max_overdue_sec=float(reward["max_overdue_sec"]),
        hard_overdue_penalty_sec=float(reward["hard_overdue_penalty_sec"]),
        hard_failure_penalty_sec=float(reward["hard_failure_penalty_sec"]),
    )


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，并断言顶层对象类型。"""
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须为对象: {path}")
    return payload


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """从字典中取出指定 mapping 字段，否则抛错。"""
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"配置缺少 mapping 段: {key}")
    return value


def _require_singleton(mapping: Mapping[str, Any], label: str) -> tuple[str, Any]:
    """断言某类实体当前只有一个，并返回该唯一元素。"""
    if len(mapping) != 1:
        raise ValueError(f"当前仅支持单 {label} 场景，实际数量={len(mapping)}")
    return next(iter(mapping.items()))


def _clone_position(pos: Position3D) -> Position3D:
    """复制 Position3D，避免直接复用实体上的同一对象引用。"""
    return Position3D(x=float(pos.x), y=float(pos.y), z=float(pos.z))


def _host_id(host: ChargingHost) -> str:
    """把 depot/station 宿主对象映射回其节点 ID。"""
    if isinstance(host, Depot):
        return host.depot_id
    if isinstance(host, SwapStation):
        return host.station_id
    raise TypeError(f"未知 host 类型: {type(host)!r}")


def _node_type_of_host(host: ChargingHost) -> str:
    """把 depot/station 宿主对象映射回当前环境使用的节点类型字符串。"""
    if isinstance(host, Depot):
        return "depot"
    if isinstance(host, SwapStation):
        return "station"
    raise TypeError(f"未知 host 类型: {type(host)!r}")


def _merge_reward_breakdown(target: dict[str, float], source: Mapping[str, float]) -> None:
    """把一段 reward breakdown 累加合并到目标字典。"""
    for key, value in source.items():
        target[key] = target.get(key, 0.0) + float(value)
