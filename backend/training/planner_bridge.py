#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — Phase 6 coarse planner bridge。

当前实现刻意保持保守：
  - 不复用 greedy / market 现有算法实现；
  - 只承担 coarse plan 刷新、触发判定与契约化输出；
  - 粗规划内容先使用稳定规则生成，后续可在不改接口的前提下替换为更强的 RH-ALNS。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import CoarsePlanView, PlannerMode, PlannerTriggerContext, PolicyMode, RouteDriftRef
from .recovery_pool_selector import select_recovery_pool_for_order
from .scene_loader import DEFAULT_CONFIG_PATH


_TIME_EPS = 1e-6


@dataclass(frozen=True)
class _PlannerConfig:
    coarse_replan_interval_sec: float
    coarse_new_order_trigger: int
    route_drift_trigger_ratio: float
    fallback_burst_trigger_count: int
    fallback_burst_window_sec: float
    hard_failure_trigger_count: int
    upper_horizon_sec: float
    support_radius_km: float
    min_orders_to_trigger: int
    max_candidate_recovery_per_order: int
    recovery_pool_future_scan_limit: int
    allow_empty_backbone_route: bool


class PlannerBridge:
    """低频 coarse plan 桥接器。"""

    def __init__(
        self,
        *,
        future_backbone_provider: Callable[[float], Sequence[Any]],
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        heavy_payload_capacity: float | None = None,
    ) -> None:
        self._cfg = _load_planner_config(Path(config_path))
        self._future_backbone_provider = future_backbone_provider
        self._heavy_payload_capacity = (
            float(heavy_payload_capacity)
            if heavy_payload_capacity is not None
            else _load_heavy_payload_capacity()
        )
        self._recovery_pool_drone_cruise_speed = (
            _load_recovery_pool_drone_cruise_speed()
        )
        self._runtime_allow_empty_backbone_route = bool(
            self._cfg.allow_empty_backbone_route
        )
        self._current_plan: CoarsePlanView | None = None

    @property
    def current_plan(self) -> CoarsePlanView | None:
        return self._current_plan

    def reset_episode(
        self,
        *,
        allow_empty_backbone_route: bool | None = None,
    ) -> None:
        """清空跨 episode coarse-plan 缓存，并同步运行时语义开关。"""
        self._current_plan = None
        if allow_empty_backbone_route is not None:
            self._runtime_allow_empty_backbone_route = bool(
                allow_empty_backbone_route
            )

    def maybe_replan(
        self,
        runtime_state: Any,
        trigger_ctx: PlannerTriggerContext,
    ) -> CoarsePlanView:
        if self._current_plan is None:
            self._current_plan = self._build_plan(
                runtime_state=runtime_state,
                t_now=float(trigger_ctx.t_now),
                plan_version=0,
            )
            return self._current_plan

        if not self._should_replan(trigger_ctx):
            return self._current_plan

        self._current_plan = self._build_plan(
            runtime_state=runtime_state,
            t_now=float(trigger_ctx.t_now),
            plan_version=self._current_plan.plan_version + 1,
        )
        return self._current_plan

    def _should_replan(self, trigger_ctx: PlannerTriggerContext) -> bool:
        assert self._current_plan is not None
        if trigger_ctx.t_now >= self._current_plan.valid_until - _TIME_EPS:
            return True
        if trigger_ctx.backlog_new_orders >= self._cfg.coarse_new_order_trigger:
            return True
        if trigger_ctx.route_drift_ratio >= self._cfg.route_drift_trigger_ratio:
            return True
        if (
            trigger_ctx.fallback_count_in_window
            >= self._cfg.fallback_burst_trigger_count
        ):
            return True
        if (
            trigger_ctx.hard_failure_count_in_window
            >= self._cfg.hard_failure_trigger_count
        ):
            return True
        return False

    def _build_plan(
        self,
        *,
        runtime_state: Any,
        t_now: float,
        plan_version: int,
    ) -> CoarsePlanView:
        future_visits = self._dedupe_future_backbone(
            self._future_backbone_provider(t_now)
        )
        truck_backbone_route = tuple(visit.node_id for visit in future_visits)
        allow_empty_backbone_route = self._runtime_allow_empty_backbone_route
        truck_eta_map = {
            visit.node_id: float(visit.arrival_time) for visit in future_visits
        }
        route_drift_ref = {
            visit.node_id: RouteDriftRef(
                eta_ref=float(visit.arrival_time),
                route_index_ref=idx,
            )
            for idx, visit in enumerate(future_visits)
        }

        authorized_orders: list[str] = []
        order_priority_band: dict[str, int] = {}
        order_pre_score: dict[str, float] = {}
        planner_mode_cap: dict[str, frozenset[PlannerMode]] = {}
        policy_mode_mask: dict[str, frozenset[PolicyMode]] = {}
        recovery_pool: dict[str, tuple[str, ...]] = {}

        pending_items = sorted(
            runtime_state.pending_orders.items(),
            key=lambda item: (float(item[1].deadline), item[0]),
        )
        for order_id, order in pending_items:
            if float(order.payload_weight) > self._heavy_payload_capacity:
                planner_mode_cap[order_id] = frozenset({PlannerMode.A})
                continue

            remaining = max(0.0, float(order.deadline) - t_now)
            window = max(_TIME_EPS, float(order.time_window_seconds))
            ratio = remaining / window
            if ratio <= (1.0 / 3.0):
                band = 0
            elif ratio <= (2.0 / 3.0):
                band = 1
            else:
                band = 2

            authorized_orders.append(order_id)
            order_priority_band[order_id] = band
            order_pre_score[order_id] = remaining
            planner_mode_cap[order_id] = frozenset({PlannerMode.B, PlannerMode.C})
            if truck_backbone_route:
                policy_mode_mask[order_id] = frozenset({PolicyMode.B, PolicyMode.C})
                recovery_pool[order_id] = select_recovery_pool_for_order(
                    order=order,
                    truck_backbone_route=truck_backbone_route,
                    truck_eta_map=truck_eta_map,
                    node_states=runtime_state.node_states,
                    max_candidates=self._cfg.max_candidate_recovery_per_order,
                    future_scan_limit=self._cfg.recovery_pool_future_scan_limit,
                    drone_cruise_speed=self._recovery_pool_drone_cruise_speed,
                )
            else:
                policy_mode_mask[order_id] = frozenset({PolicyMode.B})
                recovery_pool[order_id] = ()

        node_charge_load_budget = {
            node_id: 0 for node_id in runtime_state.node_states
        }

        launch_candidate_stations = self._select_launch_candidate_stations(
            runtime_state=runtime_state,
            truck_backbone_route=truck_backbone_route,
        )

        return CoarsePlanView(
            plan_version=plan_version,
            issued_at=float(t_now),
            valid_until=min(
                float(t_now + self._cfg.coarse_replan_interval_sec),
                float(self._cfg.upper_horizon_sec),
            ),
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
            allow_empty_backbone_route=allow_empty_backbone_route,
        )

    def _select_launch_candidate_stations(
        self,
        *,
        runtime_state: Any,
        truck_backbone_route: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not truck_backbone_route:
            return ()

        radius_m = self._cfg.support_radius_km * 1000.0
        launch_nodes: list[str] = []
        for node_id in truck_backbone_route:
            node_state = runtime_state.node_states.get(node_id)
            if node_state is None or node_state.node_type != "station":
                continue
            support_count = 0
            for order in runtime_state.pending_orders.values():
                if float(order.payload_weight) > self._heavy_payload_capacity:
                    continue
                if (
                    node_state.position.distance_2d(order.delivery_loc)
                    <= radius_m + _TIME_EPS
                ):
                    support_count += 1
                    if support_count >= self._cfg.min_orders_to_trigger:
                        launch_nodes.append(node_id)
                        break
        return tuple(launch_nodes)

    @staticmethod
    def _dedupe_future_backbone(visits: Sequence[Any]) -> tuple[Any, ...]:
        deduped: list[Any] = []
        seen_nodes: set[str] = set()
        sorted_visits = sorted(
            visits,
            key=lambda item: (float(item.arrival_time), str(item.node_id)),
        )
        for visit in sorted_visits:
            node_id = str(visit.node_id)
            if node_id in seen_nodes:
                continue
            seen_nodes.add(node_id)
            deduped.append(visit)
        return tuple(deduped)


def _load_planner_config(config_path: Path) -> _PlannerConfig:
    raw = _load_yaml(config_path)
    planner = _require_mapping(raw, "planner")
    candidate = _require_mapping(raw, "candidate")
    max_candidate_recovery_per_order = int(
        candidate["max_candidate_recovery_per_order"]
    )
    recovery_pool_future_scan_limit = int(
        candidate.get(
            "recovery_pool_future_scan_limit",
            max_candidate_recovery_per_order,
        )
    )
    if max_candidate_recovery_per_order <= 0:
        raise ValueError("candidate.max_candidate_recovery_per_order 必须为正数")
    if recovery_pool_future_scan_limit < max_candidate_recovery_per_order:
        raise ValueError(
            "candidate.recovery_pool_future_scan_limit 不能小于 "
            "max_candidate_recovery_per_order"
        )
    return _PlannerConfig(
        coarse_replan_interval_sec=float(planner["coarse_replan_interval_sec"]),
        coarse_new_order_trigger=int(planner["coarse_new_order_trigger"]),
        route_drift_trigger_ratio=float(planner["route_drift_trigger_ratio"]),
        fallback_burst_trigger_count=int(planner["fallback_burst_trigger_count"]),
        fallback_burst_window_sec=float(planner["fallback_burst_window_sec"]),
        hard_failure_trigger_count=int(planner["hard_failure_trigger_count"]),
        upper_horizon_sec=float(planner["upper_horizon_sec"]),
        support_radius_km=float(planner["support_radius_km"]),
        min_orders_to_trigger=int(planner["min_orders_to_trigger"]),
        max_candidate_recovery_per_order=max_candidate_recovery_per_order,
        recovery_pool_future_scan_limit=recovery_pool_future_scan_limit,
        allow_empty_backbone_route=bool(
            planner.get("allow_empty_backbone_route", False)
        ),
    )


def _load_heavy_payload_capacity() -> float:
    from config.loader import load_drone_params

    return float(load_drone_params().heavy.payload_capacity)


def _load_recovery_pool_drone_cruise_speed() -> float:
    from config.loader import load_drone_params

    return float(load_drone_params().light.cruise_speed)


def _load_yaml(config_path: Path) -> Mapping[str, Any]:
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("缺少 PyYAML，无法读取 planner 配置") from exc

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


__all__ = ["PlannerBridge"]
