#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — Phase 6 候选动作生成器。

输入固定为：
  - RuntimeStateView
  - CoarsePlanView
  - 本次决策的局部上下文

输出固定为：
  - CandidateFeatures
  - factorized masks
  - resolved action lookup
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.entities.primitives import Position3D

from .actions import DispatchAction, WAIT_ACTION
from .contracts import (
    CandidateFeatures,
    CandidateOutput,
    CoarsePlanView,
    FactorizedActionSchema,
    InfraFeatures,
    InfraNodeFeatures,
    OrderFeatures,
    PolicyMode,
    RecoveryFeatures,
    ResolvedActionLookup,
    UavSelfFeatures,
)
from .scene_loader import DEFAULT_CONFIG_PATH, TrainingSceneContext, load_default_scene


_TIME_EPS = 1e-6
_MODE_B_IDX = 0
_MODE_C_IDX = 1


@dataclass(frozen=True)
class _CandidateConfig:
    max_candidate_orders: int
    max_candidate_recovery_per_order: int
    max_candidate_actions: int
    station_wait_threshold_sec: float
    rendezvous_eta_safe_margin_sec: float


class CandidateBuilder:
    """无状态候选构建器。"""

    def __init__(
        self,
        *,
        scene_ctx: TrainingSceneContext | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
    ) -> None:
        self._config_path = Path(config_path)
        self._cfg = _load_candidate_config(self._config_path)
        self._scene_ctx = scene_ctx or load_default_scene(config_path=self._config_path)
        self._safe_margin_j_by_drone = {
            drone_id: float(drone.safe_margin_j)
            for drone_id, drone in self._scene_ctx.entity_manager.drones.items()
        }

    def build(
        self,
        runtime_state: Any,
        coarse_plan: CoarsePlanView,
        deciding_drone_id: str,
        trigger_type: str,
        trigger_station_id: str | None,
        last_seen_plan_version: int,
    ) -> CandidateOutput:
        _validate_trigger_context(
            runtime_state=runtime_state,
            trigger_type=trigger_type,
            trigger_station_id=trigger_station_id,
        )
        drone_view = runtime_state.drone_states[deciding_drone_id]
        launch_pos = self._resolve_launch_position(
            runtime_state=runtime_state,
            drone_view=drone_view,
            trigger_type=trigger_type,
            trigger_station_id=trigger_station_id,
        )

        actionable_orders: list[dict[str, Any]] = []
        for order_id in coarse_plan.authorized_orders:
            order = runtime_state.pending_orders.get(order_id)
            if order is None:
                continue
            allowed_policy_modes = coarse_plan.get_policy_modes(order_id)
            if not allowed_policy_modes:
                continue
            if float(order.payload_weight) > float(drone_view.payload_capacity):
                continue

            t_deliver = runtime_state.t_now + _estimate_flight_time(
                drone_view=drone_view,
                from_pos=launch_pos,
                to_pos=order.delivery_loc,
            )
            energy_to_deliver = _estimate_energy_needed(
                drone_view=drone_view,
                from_pos=launch_pos,
                to_pos=order.delivery_loc,
                payload=float(order.payload_weight),
            )
            if energy_to_deliver > float(drone_view.battery_current) + _TIME_EPS:
                continue

            energy_after_delivery = float(drone_view.battery_current) - energy_to_deliver
            mode_b_summary = None
            if PolicyMode.B in allowed_policy_modes:
                mode_b_summary = self._select_best_mode_b_host(
                    runtime_state=runtime_state,
                    drone_view=drone_view,
                    deliver_pos=order.delivery_loc,
                    battery_after_delivery=energy_after_delivery,
                )

            recovery_candidates: tuple[RecoveryFeatures, ...] = ()
            if PolicyMode.C in allowed_policy_modes:
                recovery_candidates = self._build_mode_c_candidates(
                    runtime_state=runtime_state,
                    coarse_plan=coarse_plan,
                    drone_view=drone_view,
                    order=order,
                    deliver_pos=order.delivery_loc,
                    t_deliver=t_deliver,
                    energy_after_delivery=energy_after_delivery,
                    trigger_type=trigger_type,
                    trigger_station_id=trigger_station_id,
                )

            if mode_b_summary is None and not recovery_candidates:
                continue

            order_feature = OrderFeatures(
                order_id=order_id,
                weight=float(order.payload_weight),
                deadline=float(order.deadline),
                remaining_time=max(0.0, float(order.deadline) - runtime_state.t_now),
                delivery_x=float(order.delivery_loc.x),
                delivery_y=float(order.delivery_loc.y),
                delivery_z=float(order.delivery_loc.z),
                distance_to_order=launch_pos.distance_3d(order.delivery_loc),
                order_pre_score=float(coarse_plan.order_pre_score[order_id]),
                priority_band=int(coarse_plan.order_priority_band[order_id]),
                has_mode_b_action=mode_b_summary is not None,
                best_mode_b_return_score=(
                    float(mode_b_summary["score"]) if mode_b_summary is not None else 0.0
                ),
                best_mode_b_host_type=(
                    str(mode_b_summary["host_type"]) if mode_b_summary is not None else ""
                ),
                best_mode_b_queue_time_est=(
                    float(mode_b_summary["queue_time_est"])
                    if mode_b_summary is not None
                    else 0.0
                ),
                is_valid=True,
            )
            actionable_orders.append(
                {
                    "order_id": order_id,
                    "order_feature": order_feature,
                    "has_mode_b": mode_b_summary is not None,
                    "recovery_candidates": recovery_candidates,
                }
            )

        actionable_orders.sort(
            key=lambda item: (
                item["order_feature"].order_pre_score,
                item["order_feature"].priority_band,
                item["order_id"],
            )
        )
        actionable_orders = actionable_orders[: self._cfg.max_candidate_orders]

        order_features: list[OrderFeatures] = []
        recovery_features: list[tuple[RecoveryFeatures, ...]] = []
        order_mask: list[bool] = []
        mode_mask: list[tuple[bool, bool]] = []
        recovery_mask: list[tuple[bool, ...]] = []
        dispatch_actions: dict[tuple[int, int, int | None], DispatchAction] = {}

        for order_slot, item in enumerate(actionable_orders):
            order_features.append(item["order_feature"])
            order_mask.append(True)
            recovery_rows = list(item["recovery_candidates"])
            has_mode_b = bool(item["has_mode_b"])
            has_mode_c = bool(recovery_rows)
            mode_mask.append((has_mode_b, has_mode_c))

            if has_mode_b:
                dispatch_actions[(order_slot, _MODE_B_IDX, None)] = DispatchAction(
                    order_id=item["order_id"],
                    mode=PolicyMode.B,
                )

            padded_recovery_features: list[RecoveryFeatures] = []
            padded_recovery_mask: list[bool] = []
            for recovery_slot, recovery_feature in enumerate(recovery_rows):
                padded_recovery_features.append(recovery_feature)
                padded_recovery_mask.append(True)
                dispatch_actions[(order_slot, _MODE_C_IDX, recovery_slot)] = DispatchAction(
                    order_id=item["order_id"],
                    mode=PolicyMode.C,
                    recover_node_id=recovery_feature.recover_node_id,
                )

            while len(padded_recovery_features) < self._cfg.max_candidate_recovery_per_order:
                padded_recovery_features.append(_padding_recovery_feature())
                padded_recovery_mask.append(False)

            recovery_features.append(tuple(padded_recovery_features))
            recovery_mask.append(tuple(padded_recovery_mask))

        while len(order_features) < self._cfg.max_candidate_orders:
            order_features.append(_padding_order_feature())
            recovery_features.append(
                tuple(
                    _padding_recovery_feature()
                    for _ in range(self._cfg.max_candidate_recovery_per_order)
                )
            )
            order_mask.append(False)
            mode_mask.append((False, False))
            recovery_mask.append(
                tuple(False for _ in range(self._cfg.max_candidate_recovery_per_order))
            )

        action_count = 1 + len(dispatch_actions)
        if action_count > self._cfg.max_candidate_actions:
            raise RuntimeError(
                "resolved_action_lookup 展开规模超出预算: "
                f"{action_count} > {self._cfg.max_candidate_actions}"
            )

        candidate_features = CandidateFeatures(
            uav_self=self._build_uav_self_features(
                runtime_state=runtime_state,
                coarse_plan=coarse_plan,
                drone_view=drone_view,
                last_seen_plan_version=last_seen_plan_version,
            ),
            order_features=tuple(order_features),
            recovery_features=tuple(recovery_features),
            infra_features=self._build_infra_features(runtime_state, coarse_plan),
        )
        return CandidateOutput(
            candidate_features=candidate_features,
            root_branch_mask=(True, bool(dispatch_actions)),
            has_wait_action=True,
            order_mask=tuple(order_mask),
            mode_mask=tuple(mode_mask),
            recovery_mask=tuple(recovery_mask),
            factorized_action_schema=FactorizedActionSchema(
                root_branch_order=("WAIT", "DISPATCH"),
                mode_order=("B", "C"),
                max_order_slots=self._cfg.max_candidate_orders,
                max_recovery_slots=self._cfg.max_candidate_recovery_per_order,
            ),
            resolved_action_lookup=ResolvedActionLookup(
                wait_action=WAIT_ACTION,
                dispatch_actions=dispatch_actions,
            ),
        )

    def build_from_decision_context(
        self,
        decision_context: Any,
        *,
        last_seen_plan_version: int,
    ) -> CandidateOutput:
        """基于一次既有决策快照重建 `CandidateOutput`。

        该入口保持 `CandidateOutput` 仍由 env 外部调用方持有，不挂在
        `DecisionContext` / `EnvStepResult` 上；同时避免外部重新手拼
        `runtime_state` / `coarse_plan` / trigger 字段。
        """
        try:
            runtime_state = decision_context.runtime_state
            coarse_plan = decision_context.coarse_plan
            deciding_drone_id = decision_context.deciding_drone_id
            trigger_type = decision_context.trigger_type
            trigger_station_id = decision_context.trigger_station_id
        except AttributeError as exc:
            raise TypeError(
                "decision_context 必须暴露 runtime_state / coarse_plan / "
                "deciding_drone_id / trigger_type / trigger_station_id"
            ) from exc

        return self.build(
            runtime_state=runtime_state,
            coarse_plan=coarse_plan,
            deciding_drone_id=deciding_drone_id,
            trigger_type=trigger_type,
            trigger_station_id=trigger_station_id,
            last_seen_plan_version=last_seen_plan_version,
        )

    def _resolve_launch_position(
        self,
        *,
        runtime_state: Any,
        drone_view: Any,
        trigger_type: str,
        trigger_station_id: str | None,
    ) -> Position3D:
        if (
            _is_riding_with_truck_trigger(trigger_type)
            and trigger_station_id
            and trigger_station_id in runtime_state.node_states
        ):
            return runtime_state.node_states[trigger_station_id].position
        return drone_view.current_loc

    def _build_mode_c_candidates(
        self,
        *,
        runtime_state: Any,
        coarse_plan: CoarsePlanView,
        drone_view: Any,
        order: Any,
        deliver_pos: Position3D,
        t_deliver: float,
        energy_after_delivery: float,
        trigger_type: str,
        trigger_station_id: str | None,
    ) -> tuple[RecoveryFeatures, ...]:
        if _is_riding_with_truck_trigger(trigger_type) and trigger_station_id:
            recovery_pool = self._runtime_recovery_pool(
                coarse_plan=coarse_plan,
                trigger_station_id=trigger_station_id,
            )
        else:
            recovery_pool = coarse_plan.get_recovery_candidates(order.order_id)

        safe_margin = self._safe_margin_j_by_drone[drone_view.drone_id]
        candidates: list[RecoveryFeatures] = []
        for node_id in recovery_pool:
            node_state = runtime_state.node_states.get(node_id)
            t_arrive_truck = coarse_plan.truck_eta_map.get(node_id)
            if node_state is None or t_arrive_truck is None:
                continue

            t_arrive_uav = t_deliver + _estimate_flight_time(
                drone_view=drone_view,
                from_pos=deliver_pos,
                to_pos=node_state.position,
            )
            rendezvous_margin = float(t_arrive_truck) - (
                t_arrive_uav + self._cfg.rendezvous_eta_safe_margin_sec
            )
            if rendezvous_margin < -_TIME_EPS:
                continue
            if not _can_reach(
                drone_view=drone_view,
                from_pos=deliver_pos,
                to_pos=node_state.position,
                payload=0.0,
                safe_margin=safe_margin,
                battery_current=energy_after_delivery,
            ):
                continue

            candidates.append(
                RecoveryFeatures(
                    order_id=order.order_id,
                    recover_node_id=node_id,
                    recover_node_type=str(node_state.node_type),
                    x=float(node_state.position.x),
                    y=float(node_state.position.y),
                    z=float(node_state.position.z),
                    truck_eta=float(t_arrive_truck),
                    rendezvous_margin=float(rendezvous_margin),
                    reservation_count=int(
                        runtime_state.reservation_count.get(node_id, 0)
                    ),
                    predicted_queue_time_est=_predicted_queue_time_est(node_state),
                    service_time=float(node_state.swap_time),
                    is_valid=True,
                )
            )

        candidates.sort(
            key=lambda item: (
                item.predicted_queue_time_est > self._cfg.station_wait_threshold_sec,
                item.predicted_queue_time_est,
                -item.rendezvous_margin,
                item.truck_eta,
                item.recover_node_id,
            )
        )
        return tuple(candidates[: self._cfg.max_candidate_recovery_per_order])

    def _runtime_recovery_pool(
        self,
        *,
        coarse_plan: CoarsePlanView,
        trigger_station_id: str,
    ) -> tuple[str, ...]:
        if trigger_station_id not in coarse_plan.route_drift_ref:
            return coarse_plan.truck_backbone_route
        trigger_idx = coarse_plan.get_route_position(trigger_station_id)
        return tuple(
            node_id
            for node_id in coarse_plan.truck_backbone_route
            if coarse_plan.get_route_position(node_id) >= trigger_idx
        )

    def _select_best_mode_b_host(
        self,
        *,
        runtime_state: Any,
        drone_view: Any,
        deliver_pos: Position3D,
        battery_after_delivery: float,
    ) -> dict[str, Any] | None:
        safe_margin = self._safe_margin_j_by_drone[drone_view.drone_id]
        scored_hosts: list[tuple[float, float, float, float, str, str]] = []
        for node_state in runtime_state.node_states.values():
            if not _can_reach(
                drone_view=drone_view,
                from_pos=deliver_pos,
                to_pos=node_state.position,
                payload=0.0,
                safe_margin=safe_margin,
                battery_current=battery_after_delivery,
            ):
                continue

            fly_time = _estimate_flight_time(
                drone_view=drone_view,
                from_pos=deliver_pos,
                to_pos=node_state.position,
            )
            queue_time_est = _predicted_queue_time_est(node_state)
            service_time = float(node_state.swap_time)
            score = fly_time + queue_time_est + service_time
            scored_hosts.append(
                (
                    score,
                    fly_time,
                    queue_time_est,
                    service_time,
                    str(node_state.node_id),
                    str(node_state.node_type),
                )
            )

        if not scored_hosts:
            return None

        scored_hosts.sort(key=lambda item: item[:-1])
        score, _fly_time, queue_time_est, _service_time, _node_id, host_type = scored_hosts[0]
        return {
            "score": float(score),
            "queue_time_est": float(queue_time_est),
            "host_type": host_type,
        }

    def _build_uav_self_features(
        self,
        *,
        runtime_state: Any,
        coarse_plan: CoarsePlanView,
        drone_view: Any,
        last_seen_plan_version: int,
    ) -> UavSelfFeatures:
        reservation = drone_view.reservation
        return UavSelfFeatures(
            drone_id=str(drone_view.drone_id),
            x=float(drone_view.current_loc.x),
            y=float(drone_view.current_loc.y),
            z=float(drone_view.current_loc.z),
            battery_current=float(drone_view.battery_current),
            battery_max=float(drone_view.battery_max),
            battery_ratio=float(drone_view.battery_ratio),
            training_state=str(drone_view.training_state),
            has_reservation=reservation is not None,
            reservation_remaining_sec=(
                max(0.0, float(reservation.expires_at) - float(runtime_state.t_now))
                if reservation is not None
                else 0.0
            ),
            plan_version_delta=int(coarse_plan.plan_version - last_seen_plan_version),
            is_riding_truck=str(drone_view.training_state) == "riding_with_truck",
            drone_source_type=str(drone_view.home_type),
            cruise_speed=float(drone_view.cruise_speed),
            payload_capacity=float(drone_view.payload_capacity),
        )

    def _build_infra_features(
        self,
        runtime_state: Any,
        coarse_plan: CoarsePlanView,
    ) -> InfraFeatures:
        node_features = tuple(
            InfraNodeFeatures(
                node_id=str(node_id),
                node_type=str(node_state.node_type),
                x=float(node_state.position.x),
                y=float(node_state.position.y),
                z=float(node_state.position.z),
                queue_length=int(node_state.queue_length),
                available_slots=int(node_state.available_slots),
                parking_slots=int(node_state.parking_slots),
                swap_time=float(node_state.swap_time),
                truck_eta=(
                    float(coarse_plan.truck_eta_map[node_id])
                    if node_id in coarse_plan.truck_eta_map
                    else None
                ),
                node_charge_load_budget=int(
                    coarse_plan.node_charge_load_budget.get(node_id, 0)
                ),
                is_in_backbone=node_id in coarse_plan.truck_eta_map,
                is_launch_candidate_station=coarse_plan.is_launch_candidate_station(
                    node_id
                ),
            )
            for node_id, node_state in sorted(runtime_state.node_states.items())
        )
        return InfraFeatures(
            truck_x=float(runtime_state.truck_current_loc.x),
            truck_y=float(runtime_state.truck_current_loc.y),
            truck_z=float(runtime_state.truck_current_loc.z),
            plan_version=int(coarse_plan.plan_version),
            future_backbone_node_count=len(coarse_plan.truck_backbone_route),
            authorized_order_count=len(coarse_plan.authorized_orders),
            node_features=node_features,
        )


def _estimate_flight_time(
    *,
    drone_view: Any,
    from_pos: Position3D,
    to_pos: Position3D,
) -> float:
    return from_pos.distance_3d(to_pos) / max(_TIME_EPS, float(drone_view.cruise_speed))


def _estimate_energy_needed(
    *,
    drone_view: Any,
    from_pos: Position3D,
    to_pos: Position3D,
    payload: float,
) -> float:
    distance = from_pos.distance_3d(to_pos)
    if distance <= _TIME_EPS:
        return 0.0
    speed = max(_TIME_EPS, float(drone_view.cruise_speed))
    power = float(drone_view.k1) * (
        float(drone_view.empty_weight) + max(0.0, payload)
    ) ** 1.5 + float(drone_view.k2) * speed**3
    return power * (distance / speed)


def _can_reach(
    *,
    drone_view: Any,
    from_pos: Position3D,
    to_pos: Position3D,
    payload: float,
    safe_margin: float,
    battery_current: float,
) -> bool:
    return battery_current + _TIME_EPS >= (
        _estimate_energy_needed(
            drone_view=drone_view,
            from_pos=from_pos,
            to_pos=to_pos,
            payload=payload,
        )
        + safe_margin
    )


def _predicted_queue_time_est(node_state: Any) -> float:
    if int(node_state.available_slots) > 0:
        return 0.0
    return (int(node_state.queue_length) + 1) * float(node_state.swap_time)


def _is_riding_with_truck_trigger(trigger_type: str) -> bool:
    """兼容文档对外语义名与 env 内部事件名。"""
    return trigger_type in {"riding_with_truck", "truck_station_arrival"}


def _validate_trigger_context(
    *,
    runtime_state: Any,
    trigger_type: str,
    trigger_station_id: str | None,
) -> None:
    if not _is_riding_with_truck_trigger(trigger_type):
        return
    if not trigger_station_id:
        raise ValueError(
            "riding_with_truck / truck_station_arrival 触发必须提供 trigger_station_id"
        )
    if trigger_station_id not in runtime_state.node_states:
        raise ValueError(
            "trigger_station_id 不存在于 runtime_state.node_states: "
            f"{trigger_station_id}"
        )


def _padding_order_feature() -> OrderFeatures:
    return OrderFeatures(
        order_id="",
        weight=0.0,
        deadline=0.0,
        remaining_time=0.0,
        delivery_x=0.0,
        delivery_y=0.0,
        delivery_z=0.0,
        distance_to_order=0.0,
        order_pre_score=0.0,
        priority_band=0,
        has_mode_b_action=False,
        best_mode_b_return_score=0.0,
        best_mode_b_host_type="",
        best_mode_b_queue_time_est=0.0,
        is_valid=False,
    )


def _padding_recovery_feature() -> RecoveryFeatures:
    return RecoveryFeatures(
        order_id="",
        recover_node_id="",
        recover_node_type="",
        x=0.0,
        y=0.0,
        z=0.0,
        truck_eta=0.0,
        rendezvous_margin=0.0,
        reservation_count=0,
        predicted_queue_time_est=0.0,
        service_time=0.0,
        is_valid=False,
    )


def _load_candidate_config(config_path: Path) -> _CandidateConfig:
    raw = _load_yaml(config_path)
    candidate = _require_mapping(raw, "candidate")
    return _CandidateConfig(
        max_candidate_orders=int(candidate["max_candidate_orders"]),
        max_candidate_recovery_per_order=int(
            candidate["max_candidate_recovery_per_order"]
        ),
        max_candidate_actions=int(candidate["max_candidate_actions"]),
        station_wait_threshold_sec=float(candidate["station_wait_threshold_sec"]),
        rendezvous_eta_safe_margin_sec=float(
            candidate["rendezvous_eta_safe_margin_sec"]
        ),
    )


def _load_yaml(config_path: Path) -> Mapping[str, Any]:
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("缺少 PyYAML，无法读取 candidate 配置") from exc

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, Mapping):
        raise ValueError(f"YAML 顶层必须为 mapping: {config_path}")
    return raw


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"配置缺少 mapping 段: {key}")
    return value


__all__ = ["CandidateBuilder"]
