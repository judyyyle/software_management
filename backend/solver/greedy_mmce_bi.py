#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — MMCE Backbone Insertion

在 GreedyMMCE 基础上实现“骨干路径 + 增量插入”策略：
1) 静态批次：先构建卡车骨干路径（重货单），再增量插入其余订单。
2) 动态批次：基于卡车当前未执行后缀路线做订单插入，不做全局重排。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from core.entities.primitives import Position3D
from solver.greedy_mmce import AllocationResult, DispatchPlan, GreedyMMCE, TruckRoute

if TYPE_CHECKING:
    from core.entities.drone import Drone
    from core.entities.order import Order
    from core.entities.truck import Truck


logger = logging.getLogger(__name__)


@dataclass
class _InsertionChoice:
    """模式 A 增量插入结果。"""

    truck_id: str
    insert_idx: int
    delta_score: float
    cost_dist: float
    cost_energy: float
    cost_penalty: float
    distance: float


@dataclass
class _ModeBCandidate:
    """模式 B 候选（含插站动作）。"""

    truck_id: str
    drone_id: str
    launch_station_id: str
    launch_insert_idx: int
    recovery_station_id: str
    recovery_insert_idx: int
    launch_time: float
    wait_duration: float
    delta_score: float
    cost_dist: float
    cost_energy: float
    cost_penalty: float
    distance: float
    detour_ratio: float


@dataclass
class _ModeBDiagnostics:
    """模式 B 枚举诊断信息，用于解释为何未入候选。"""

    has_drone: bool = True
    has_station: bool = True
    total_station_trials: int = 0
    scenario_feasible_trials: int = 0
    rejected_detour: int = 0
    rejected_score: int = 0
    accepted_trials: int = 0
    best_raw_score: float = math.inf
    best_raw_detour_ratio: float = math.inf
    best_raw_cost_dist: float = 0.0
    best_raw_cost_energy: float = 0.0
    best_raw_cost_penalty: float = 0.0


@dataclass
class _StationLaunchOption:
    """B_WAIT 出发站候选（含在当前未来路线中的最佳插入信息）。"""

    station_id: str
    station_pos: Position3D
    launch_insert_idx: int
    launch_extra: float
    launch_path_distance: float
    launch_eta: float
    task_distance: float
    source: str


class GreedyMMCEBackboneInsertion(GreedyMMCE):
    """MMCE-BI：骨干构建 + 多模式增量插入。"""

    # 模式 B 合法性阈值：路径长度增幅上限（10%~20% 通常可接受）
    B_DETOUR_RATIO_LIMIT = 0.15
    # 骨干插站仅允许“近似顺路”而非明显绕路。
    BACKBONE_STATION_DETOUR_LIMIT = 0.02
    BACKBONE_STATION_MAX_EXTRA_M = 30.0
    B_WAIT_ROUTE_TOP_K = 3
    B_WAIT_TASK_TOP_K = 3
    B_WAIT_ETA_TOP_K = 3
    B_WAIT_MAX_CANDIDATES = 8
    WAIT_PENALTY_TIME_SCALE_S = 60.0

    def _nearest_station_for_pos(self, pos: Position3D) -> tuple[str, Position3D] | None:
        """返回距离给定位置最近的充电站 (station_id, location)。"""
        stations = list(self.entity_mgr.stations.values())
        if not stations:
            return None
        best = min(stations, key=lambda s: self._dist(pos, s.location))
        return best.station_id, best.location

    def _terminal_anchor_pos(self, pos: Position3D) -> Position3D | None:
        """终点锚点：优先最近充电站，缺失时回退仓库。"""
        nearest_station = self._nearest_station_for_pos(pos)
        if nearest_station is not None:
            return nearest_station[1]
        depots = list(self.entity_mgr.depots.values())
        if depots:
            return depots[0].location
        return None

    def _estimate_distance_to_nearest_depot(self, pos: Position3D) -> float:
        """重载基类口径：MMCE-BI 末端锚点改为最近充电站（无站时回退仓库）。"""
        anchor = self._terminal_anchor_pos(pos)
        if anchor is None:
            return 0.0
        return self._road_dist(pos, anchor)

    def should_replan_unfinished(self) -> bool:
        """MMCE-BI 默认走纯增量（新单插入），不触发全局重排。"""
        return False

    def dispatch(
        self,
        pending_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """静态调度：重货骨干 + 其余订单增量插入。"""
        return self._dispatch_backbone_insertion(
            orders=pending_orders,
            current_time=current_time,
            bbox=bbox,
            scene_id=scene_id,
            incremental=False,
            start_from_current_state=False,
            dispatch_type_override="full",
        )

    def dispatch_incremental(
        self,
        new_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """动态调度：在既有后缀路径上插入新订单。"""
        return self._dispatch_backbone_insertion(
            orders=new_orders,
            current_time=current_time,
            bbox=bbox,
            scene_id=scene_id,
            incremental=True,
            start_from_current_state=True,
            dispatch_type_override="incremental",
        )

    def dispatch_replan_current_state(
        self,
        replan_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """兼容协议：保持插入式策略，不做全局重排。"""
        return self._dispatch_backbone_insertion(
            orders=replan_orders,
            current_time=current_time,
            bbox=bbox,
            scene_id=scene_id,
            incremental=True,
            start_from_current_state=True,
            dispatch_type_override="dynamic_replan",
        )

    def _dispatch_backbone_insertion(
        self,
        orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None,
        incremental: bool,
        start_from_current_state: bool,
        dispatch_type_override: str,
    ) -> DispatchPlan:
        if not orders:
            return DispatchPlan(
                allocations=[],
                cost_total=0.0,
                summary={
                    "total_orders": 0,
                    "feasible": 0,
                    "modes": {},
                    "dispatch_type": dispatch_type_override,
                    "cost_breakdown": {"dist": 0.0, "energy": 0.0, "penalty": 0.0},
                },
            )

        self._road_distance_memo.clear()
        self._load_road_graph(bbox, scene_id)

        sorted_orders = sorted(
            orders.values(),
            key=lambda o: (o.deadline - current_time, o.deadline),
        )

        stops_by_truck = self._init_truck_stops(current_time, incremental)
        allocations: list[AllocationResult] = []
        mode_counter: dict[str, int] = {}
        changed_trucks: set[str] = set()
        allocated_drones: set[str] = set()
        recovery_wait_times: dict[tuple[str, str], float] = {}

        if incremental:
            heavy_orders = [o for o in sorted_orders if self._is_truck_only_order(o)]
            flex_orders = [o for o in sorted_orders if not self._is_truck_only_order(o)]
        else:
            heavy_orders = [o for o in sorted_orders if self._is_truck_only_order(o)]
            flex_orders = [o for o in sorted_orders if not self._is_truck_only_order(o)]

        # 阶段 1：重货订单先形成骨干路径。
        for order in heavy_orders:
            choice = self._best_truck_only_insertion(
                order,
                current_time,
                stops_by_truck,
                start_from_current_state,
            )
            if choice is None:
                allocations.append(
                    AllocationResult(
                        order_id=order.order_id,
                        vehicle_id="",
                        mode="REJECT",
                        distance=float("inf"),
                        feasible=False,
                        reason="无可用卡车执行重货订单",
                    )
                )
                continue

            prev_id, next_id, pos_tag = self._describe_insert_position(
                stops_by_truck[choice.truck_id],
                choice.insert_idx,
            )
            self._insert_customer_stop(stops_by_truck[choice.truck_id], order, choice.insert_idx)
            changed_trucks.add(choice.truck_id)
            mode_counter["A"] = mode_counter.get("A", 0) + 1
            logger.info(
                "[MMCE-BI] 重货订单 %s 骨干插入: mode=A score=%.2f "
                "(dist=%.2f, energy=%.2f, penalty=%.2f)",
                order.order_id,
                choice.delta_score,
                choice.cost_dist,
                choice.cost_energy,
                choice.cost_penalty,
            )
            logger.info(
                "[MMCE-BI] 订单 %s 卡车插入位: truck=%s idx=%d (%s) prev=%s next=%s",
                order.order_id,
                choice.truck_id,
                choice.insert_idx,
                pos_tag,
                prev_id,
                next_id,
            )
            allocations.append(
                AllocationResult(
                    order_id=order.order_id,
                    vehicle_id=choice.truck_id,
                    mode="A",
                    distance=choice.distance,
                    feasible=True,
                    score_total=choice.delta_score,
                    cost_dist=choice.cost_dist,
                    cost_energy=choice.cost_energy,
                    cost_penalty=choice.cost_penalty,
                )
            )

        # 阶段 1.5：沿骨干路径补可达充电结构。
        if not incremental:
            for truck_id, stops in stops_by_truck.items():
                if not stops:
                    continue
                added = self._augment_backbone_stations(
                    truck_id,
                    stops,
                    current_time,
                    start_from_current_state=False,
                    return_to_depot=True,
                )
                if added:
                    changed_trucks.add(truck_id)

        # 阶段 2：其余订单按 A/B/C 候选增量插入。
        for order in flex_orders:
            a_choice = self._best_truck_only_insertion(
                order,
                current_time,
                stops_by_truck,
                start_from_current_state,
            )
            c_alloc = self._try_mode_c(order, current_time, allocated_drones, {})
            c_score = float("inf")
            if c_alloc.feasible:
                c_score, c_dist, c_energy, c_penalty = self._score_allocation(c_alloc, order, current_time, {})
                c_alloc.score_total = c_score
                c_alloc.cost_dist = c_dist
                c_alloc.cost_energy = c_energy
                c_alloc.cost_penalty = c_penalty

            b_choice, b_diag = self._best_mode_b_insertion(
                order=order,
                current_time=current_time,
                stops_by_truck=stops_by_truck,
                start_from_current_state=start_from_current_state,
                allocated_drones=allocated_drones,
                a_baseline=(a_choice.delta_score if a_choice is not None else float("inf")),
            )

            if b_choice is None:
                if not b_diag.has_drone:
                    logger.info(
                        "[MMCE-BI] 订单 %s B_WAIT 未入候选: 无可用无人机",
                        order.order_id,
                    )
                elif not b_diag.has_station:
                    logger.info(
                        "[MMCE-BI] 订单 %s B_WAIT 未入候选: 无可用充电站",
                        order.order_id,
                    )
                else:
                    raw_score_text = (
                        "inf"
                        if not math.isfinite(b_diag.best_raw_score)
                        else f"{b_diag.best_raw_score:.2f}"
                    )
                    raw_detour_text = (
                        "inf"
                        if not math.isfinite(b_diag.best_raw_detour_ratio)
                        else f"{b_diag.best_raw_detour_ratio:.3f}"
                    )
                    raw_dist_text = (
                        "inf"
                        if not math.isfinite(b_diag.best_raw_score)
                        else f"{b_diag.best_raw_cost_dist:.2f}"
                    )
                    raw_energy_text = (
                        "inf"
                        if not math.isfinite(b_diag.best_raw_score)
                        else f"{b_diag.best_raw_cost_energy:.2f}"
                    )
                    raw_penalty_text = (
                        "inf"
                        if not math.isfinite(b_diag.best_raw_score)
                        else f"{b_diag.best_raw_cost_penalty:.2f}"
                    )
                    a_text = (
                        "inf"
                        if a_choice is None or not math.isfinite(a_choice.delta_score)
                        else f"{a_choice.delta_score:.2f}"
                    )
                    logger.info(
                        "[MMCE-BI] 订单 %s B_WAIT 未入候选: 枚举=%d, 可行=%d, "
                        "detour拒绝=%d, 分数拒绝=%d, "
                        "最佳raw(score=%s, dist=%s, energy=%s, penalty=%s, detour=%s), "
                        "A基线=%s",
                        order.order_id,
                        b_diag.total_station_trials,
                        b_diag.scenario_feasible_trials,
                        b_diag.rejected_detour,
                        b_diag.rejected_score,
                        raw_score_text,
                        raw_dist_text,
                        raw_energy_text,
                        raw_penalty_text,
                        raw_detour_text,
                        a_text,
                    )

            candidate_items: list[dict[str, float | str]] = []
            candidate_metrics: dict[str, tuple[float, float, float, float]] = {}
            if a_choice is not None:
                candidate_items.append(
                    {
                        "mode": "A",
                        "score": round(a_choice.delta_score, 2),
                        "dist": round(a_choice.cost_dist, 2),
                        "energy": round(a_choice.cost_energy, 2),
                        "penalty": round(a_choice.cost_penalty, 2),
                    }
                )
                candidate_metrics["A"] = (
                    a_choice.delta_score,
                    a_choice.cost_dist,
                    a_choice.cost_energy,
                    a_choice.cost_penalty,
                )
            if b_choice is not None:
                candidate_items.append(
                    {
                        "mode": "B_WAIT",
                        "score": round(b_choice.delta_score, 2),
                        "dist": round(b_choice.cost_dist, 2),
                        "energy": round(b_choice.cost_energy, 2),
                        "penalty": round(b_choice.cost_penalty, 2),
                    }
                )
                candidate_metrics["B"] = (
                    b_choice.delta_score,
                    b_choice.cost_dist,
                    b_choice.cost_energy,
                    b_choice.cost_penalty,
                )
            if c_alloc.feasible:
                candidate_items.append(
                    {
                        "mode": "C",
                        "score": round(c_alloc.score_total, 2),
                        "dist": round(c_alloc.cost_dist, 2),
                        "energy": round(c_alloc.cost_energy, 2),
                        "penalty": round(c_alloc.cost_penalty, 2),
                    }
                )
                candidate_metrics["C"] = (
                    c_alloc.score_total,
                    c_alloc.cost_dist,
                    c_alloc.cost_energy,
                    c_alloc.cost_penalty,
                )

            candidate_scores = {
                "A": a_choice.delta_score if a_choice is not None else float("inf"),
                "B": b_choice.delta_score if b_choice is not None else float("inf"),
                "C": c_score,
            }
            best_mode = min(candidate_scores, key=candidate_scores.get)
            best_score = candidate_scores[best_mode]

            if not math.isfinite(best_score):
                allocations.append(
                    AllocationResult(
                        order_id=order.order_id,
                        vehicle_id="",
                        mode="REJECT",
                        distance=float("inf"),
                        feasible=False,
                        reason="A/B/C 均不可行",
                    )
                )
                logger.info(
                    "[MMCE-BI] 订单 %s 候选评分: %s | 选中=REJECT",
                    order.order_id,
                    candidate_items,
                )
                continue

            selected_score, selected_dist, selected_energy, selected_penalty = candidate_metrics[best_mode]
            selected_mode_name = "B_WAIT" if best_mode == "B" else best_mode
            logger.info(
                "[MMCE-BI] 订单 %s 候选评分: %s | 选中=%s score=%.2f "
                "(dist=%.2f, energy=%.2f, penalty=%.2f)",
                order.order_id,
                candidate_items,
                selected_mode_name,
                selected_score,
                selected_dist,
                selected_energy,
                selected_penalty,
            )

            if best_mode == "A" and a_choice is not None:
                prev_id, next_id, pos_tag = self._describe_insert_position(
                    stops_by_truck[a_choice.truck_id],
                    a_choice.insert_idx,
                )
                self._insert_customer_stop(stops_by_truck[a_choice.truck_id], order, a_choice.insert_idx)
                changed_trucks.add(a_choice.truck_id)
                mode_counter["A"] = mode_counter.get("A", 0) + 1
                logger.info(
                    "[MMCE-BI] 订单 %s 卡车插入位: truck=%s idx=%d (%s) prev=%s next=%s",
                    order.order_id,
                    a_choice.truck_id,
                    a_choice.insert_idx,
                    pos_tag,
                    prev_id,
                    next_id,
                )
                allocations.append(
                    AllocationResult(
                        order_id=order.order_id,
                        vehicle_id=a_choice.truck_id,
                        mode="A",
                        distance=a_choice.distance,
                        feasible=True,
                        score_total=a_choice.delta_score,
                        cost_dist=a_choice.cost_dist,
                        cost_energy=a_choice.cost_energy,
                        cost_penalty=a_choice.cost_penalty,
                    )
                )
                continue

            if best_mode == "B" and b_choice is not None:
                truck_stops = stops_by_truck[b_choice.truck_id]
                launch_pos = self.entity_mgr.stations[b_choice.launch_station_id].location
                recovery_entity = (
                    self.entity_mgr.stations.get(b_choice.recovery_station_id)
                    or self.entity_mgr.depots.get(b_choice.recovery_station_id)
                )
                recovery_pos = recovery_entity.location if recovery_entity is not None else launch_pos

                launch_prev, launch_next, launch_tag = self._describe_insert_position(
                    truck_stops,
                    b_choice.launch_insert_idx,
                )
                launch_idx = self._ensure_recovery_stop(
                    stops=truck_stops,
                    station_id=b_choice.launch_station_id,
                    station_pos=launch_pos,
                    suggested_idx=b_choice.launch_insert_idx,
                    min_service=self.TRUCK_DRONE_LAUNCH_TIME,
                )
                logger.info(
                    "[MMCE-BI] 订单 %s B_WAIT 放飞站插入: truck=%s station=%s idx=%d (%s) prev=%s next=%s",
                    order.order_id,
                    b_choice.truck_id,
                    b_choice.launch_station_id,
                    launch_idx,
                    launch_tag,
                    launch_prev,
                    launch_next,
                )
                recovery_idx = launch_idx
                if b_choice.recovery_station_id != b_choice.launch_station_id:
                    recovery_prev, recovery_next, recovery_tag = self._describe_insert_position(
                        truck_stops,
                        max(b_choice.recovery_insert_idx, launch_idx + 1),
                    )
                    recovery_idx = self._ensure_recovery_stop(
                        stops=truck_stops,
                        station_id=b_choice.recovery_station_id,
                        station_pos=recovery_pos,
                        suggested_idx=max(b_choice.recovery_insert_idx, launch_idx + 1),
                        min_service=b_choice.wait_duration,
                    )
                    logger.info(
                        "[MMCE-BI] 订单 %s B_WAIT 回收站插入: truck=%s station=%s idx=%d (%s) prev=%s next=%s",
                        order.order_id,
                        b_choice.truck_id,
                        b_choice.recovery_station_id,
                        recovery_idx,
                        recovery_tag,
                        recovery_prev,
                        recovery_next,
                    )
                else:
                    truck_stops[launch_idx]["service_time"] = max(
                        float(truck_stops[launch_idx].get("service_time", 0.0)),
                        b_choice.wait_duration,
                    )

                changed_trucks.add(b_choice.truck_id)
                allocated_drones.add(b_choice.drone_id)
                recovery_wait_times[(b_choice.truck_id, b_choice.recovery_station_id)] = max(
                    recovery_wait_times.get((b_choice.truck_id, b_choice.recovery_station_id), 0.0),
                    b_choice.wait_duration,
                )
                mode_counter["B_WAIT"] = mode_counter.get("B_WAIT", 0) + 1
                allocations.append(
                    AllocationResult(
                        order_id=order.order_id,
                        vehicle_id=b_choice.truck_id,
                        mode="B_WAIT",
                        distance=b_choice.distance,
                        feasible=True,
                        recovery_station_id=b_choice.recovery_station_id,
                        drone_id=b_choice.drone_id,
                        launch_station_id=b_choice.launch_station_id,
                        launch_time=b_choice.launch_time,
                        wait_duration=b_choice.wait_duration,
                        score_total=b_choice.delta_score,
                        cost_dist=b_choice.cost_dist,
                        cost_energy=b_choice.cost_energy,
                        cost_penalty=b_choice.cost_penalty,
                    )
                )
                continue

            # best_mode == "C"
            mode_counter["C"] = mode_counter.get("C", 0) + 1
            allocations.append(c_alloc)
            allocated_drones.add(c_alloc.drone_id)

        # 清理“非任务必需”的 station 节点，避免无意义停靠（如末尾先去站点再回仓）。
        self._prune_nonessential_station_stops(stops_by_truck, allocations)

        truck_routes: dict[str, TruckRoute] = {}
        route_trucks = changed_trucks if incremental else {tid for tid, s in stops_by_truck.items() if s}

        for truck_id in route_trucks:
            truck = self.entity_mgr.trucks.get(truck_id)
            if truck is None:
                continue

            stops = stops_by_truck.get(truck_id, [])
            if not stops:
                continue

            # 终点锚定到“最后任务点最近充电站”，不再回仓。
            if self._ensure_terminal_station_stop(stops):
                changed_trucks.add(truck_id)

            route_stops = [
                {
                    "node_id": stop["node_id"],
                    "node_type": stop["node_type"],
                    "position": stop["position"],
                    "arrival_time": current_time,
                    "departure_time": current_time + max(0.0, float(stop.get("service_time", 0.0))),
                    "order_id": stop.get("order_id", ""),
                }
                for stop in stops
            ]

            rebuilt = self.build_incremental_route_from_stops(
                truck=truck,
                ordered_stops=route_stops,
                current_time=current_time,
            )
            if rebuilt is None:
                continue

            # recovery 节点服务时长按本轮分配刷新，避免等待被短路。
            for node in rebuilt.nodes:
                if node.node_type != "recovery":
                    continue
                wait_s = recovery_wait_times.get((truck_id, node.node_id))
                if wait_s is None:
                    continue
                node.departure_time = max(node.departure_time, node.arrival_time + wait_s)

            truck_routes[truck_id] = rebuilt

        cost_dist_total, cost_energy_total, cost_total = self._recalculate_plan_route_costs(
            allocations,
            truck_routes,
            current_time,
            orders,
        )
        cost_penalty_total = sum(a.cost_penalty for a in allocations if a.feasible)

        summary = {
            "total_orders": len(allocations),
            "feasible": sum(1 for a in allocations if a.feasible),
            "modes": mode_counter,
            "dispatch_type": dispatch_type_override,
            "cost_breakdown": {
                "dist": cost_dist_total,
                "energy": cost_energy_total,
                "penalty": cost_penalty_total,
            },
        }
        logger.info("[MMCE-BI] 分配完成：%s", summary)
        return DispatchPlan(
            allocations=allocations,
            cost_total=cost_total,
            summary=summary,
            truck_routes=truck_routes,
        )

    def _init_truck_stops(self, current_time: float, incremental: bool) -> dict[str, list[dict]]:
        """初始化每辆卡车的可插入停靠序列。"""
        result: dict[str, list[dict]] = {}
        for truck_id, truck in self.entity_mgr.trucks.items():
            seq: list[dict] = []
            if incremental:
                planned = getattr(truck, "_planned_route_stops", None) or []
                cursor = int(getattr(truck, "_planned_route_cursor", 0) or 0)
                for stop in planned[cursor:]:
                    node_type = stop.get("node_type", "")
                    if node_type not in ("customer", "recovery", "station"):
                        continue
                    position = stop.get("position")
                    if position is None:
                        continue
                    arr = float(stop.get("arrival_time", current_time))
                    dep = float(stop.get("departure_time", arr))
                    seq.append(
                        {
                            "node_id": stop.get("node_id", ""),
                            "node_type": node_type,
                            "position": position,
                            "order_id": stop.get("order_id", ""),
                            "service_time": max(0.0, dep - arr),
                        }
                    )
            result[truck_id] = seq
        return result

    def _is_truck_only_order(self, order: "Order") -> bool:
        """重货判定：超出系统内任意无人机载重上限。"""
        if not self.entity_mgr.drones:
            return True
        max_payload = max((float(d.payload_capacity) for d in self.entity_mgr.drones.values()), default=0.0)
        return float(order.payload_weight) > max_payload

    def _get_route_start_pos(self, truck: "Truck", current_time: float, start_from_current_state: bool) -> Position3D:
        """返回插入评估的路线起点。"""
        if start_from_current_state:
            return truck.get_location(current_time)
        depots = list(self.entity_mgr.depots.values())
        if depots:
            return depots[0].location
        return truck.get_location(current_time)

    def _sequence_distance(
        self,
        truck: "Truck",
        stops: list[dict],
        current_time: float,
        start_from_current_state: bool,
        return_to_depot: bool,
    ) -> float:
        """计算当前停靠序列总里程（用于 detour 比率）。"""
        total = 0.0
        cur = self._get_route_start_pos(truck, current_time, start_from_current_state)
        for stop in stops:
            total += self._road_dist(cur, stop["position"])
            cur = stop["position"]
        if return_to_depot:
            depots = list(self.entity_mgr.depots.values())
            if depots:
                total += self._road_dist(cur, depots[0].location)
        return total

    def _best_truck_only_insertion(
        self,
        order: "Order",
        current_time: float,
        stops_by_truck: dict[str, list[dict]],
        start_from_current_state: bool,
    ) -> Optional[_InsertionChoice]:
        """枚举所有 (truck, i, j) 插入点，返回模式 A 最小增量。"""
        best: Optional[_InsertionChoice] = None

        for truck_id, truck in self.entity_mgr.trucks.items():
            seq = stops_by_truck.get(truck_id, [])
            start_pos = self._get_route_start_pos(truck, current_time, start_from_current_state)
            return_to_depot = True
            base_len = self._sequence_distance(
                truck,
                seq,
                current_time,
                start_from_current_state,
                return_to_depot,
            )

            for idx in range(len(seq) + 1):
                prev_pos = start_pos if idx == 0 else seq[idx - 1]["position"]

                if idx < len(seq):
                    next_pos = seq[idx]["position"]
                    replaced = self._road_dist(prev_pos, next_pos)
                else:
                    next_pos = None
                    base_anchor = self._terminal_anchor_pos(prev_pos)
                    if base_anchor is None:
                        replaced = 0.0
                    else:
                        replaced = self._road_dist(prev_pos, base_anchor)

                added = self._road_dist(prev_pos, order.delivery_loc)
                if idx < len(seq) and next_pos is not None:
                    added += self._road_dist(order.delivery_loc, next_pos)
                else:
                    new_anchor = self._terminal_anchor_pos(order.delivery_loc)
                    if new_anchor is not None:
                        added += self._road_dist(order.delivery_loc, new_anchor)
                delta_dist = max(0.0, added - replaced)

                speed = max(1e-6, float(getattr(truck, "speed", 0.0)))
                delivery_time_est = current_time + (base_len + delta_dist) / speed + self.SERVICE_TIME_CUSTOMER
                lateness = max(0.0, delivery_time_est - order.deadline)
                cost_dist = self.C_DIST_ET * delta_dist
                cost_energy = self.C_ENERGY_ET * self._truck_energy_wh(delta_dist)
                cost_penalty = self.LAMBDA_TIME * order.penalty_rate * lateness
                delta_score = cost_dist + cost_energy + cost_penalty

                cand = _InsertionChoice(
                    truck_id=truck_id,
                    insert_idx=idx,
                    delta_score=delta_score,
                    cost_dist=cost_dist,
                    cost_energy=cost_energy,
                    cost_penalty=cost_penalty,
                    distance=delta_dist,
                )
                if best is None or cand.delta_score < best.delta_score:
                    best = cand

        return best

    def _insert_customer_stop(self, seq: list[dict], order: "Order", idx: int) -> None:
        """在指定位置插入 customer 节点。"""
        stop = {
            "node_id": order.order_id,
            "node_type": "customer",
            "position": order.delivery_loc,
            "order_id": order.order_id,
            "service_time": self.SERVICE_TIME_CUSTOMER,
        }
        seq.insert(max(0, min(idx, len(seq))), stop)

    @staticmethod
    def _describe_insert_position(seq: list[dict], idx: int) -> tuple[str, str, str]:
        """描述插入位置：返回 (prev_id, next_id, 头/中/尾标签)。"""
        pos = max(0, min(idx, len(seq)))
        prev_id = "ORIGIN" if pos == 0 else str(seq[pos - 1].get("node_id", ""))
        next_id = "END" if pos >= len(seq) else str(seq[pos].get("node_id", ""))
        if pos == 0:
            tag = "head"
        elif pos == len(seq):
            tag = "tail"
        else:
            tag = "middle"
        return prev_id, next_id, tag

    def _best_station_insertion(
        self,
        truck: "Truck",
        seq: list[dict],
        station_pos: Position3D,
        current_time: float,
        start_from_current_state: bool,
        return_to_depot: bool,
    ) -> tuple[int, float]:
        """返回某站点最佳插入位置与附加里程。"""
        start_pos = self._get_route_start_pos(truck, current_time, start_from_current_state)
        best_idx = 0
        best_extra = float("inf")

        for idx in range(len(seq) + 1):
            prev_pos = start_pos if idx == 0 else seq[idx - 1]["position"]
            if idx < len(seq):
                next_pos = seq[idx]["position"]
                replaced = self._road_dist(prev_pos, next_pos)
                extra = self._road_dist(prev_pos, station_pos) + self._road_dist(station_pos, next_pos) - replaced
            else:
                base_anchor = self._terminal_anchor_pos(prev_pos)
                new_anchor = self._terminal_anchor_pos(station_pos)
                if base_anchor is None or new_anchor is None:
                    extra = self._road_dist(prev_pos, station_pos)
                else:
                    replaced = self._road_dist(prev_pos, base_anchor)
                    extra = (
                        self._road_dist(prev_pos, station_pos)
                        + self._road_dist(station_pos, new_anchor)
                        - replaced
                    )

            if extra < best_extra:
                best_extra = extra
                best_idx = idx

        return best_idx, max(0.0, best_extra)

    def _best_mode_b_insertion(
        self,
        order: "Order",
        current_time: float,
        stops_by_truck: dict[str, list[dict]],
        start_from_current_state: bool,
        allocated_drones: set[str],
        a_baseline: float,
    ) -> tuple[Optional[_ModeBCandidate], _ModeBDiagnostics]:
        """枚举 i/(i,j)+s 方案，应用 B 约束后返回最优候选。"""
        diag = _ModeBDiagnostics()
        available_drones = [
            d for d in self._get_available_drones()
            if d.drone_id not in allocated_drones
        ]
        drone = self._find_capable_drone(order.payload_weight, available_drones)
        if drone is None:
            diag.has_drone = False
            return None, diag

        stations = list(self.entity_mgr.stations.values())
        if not stations:
            diag.has_station = False
            return None, diag

        recovery_pool = self._get_recovery_pool()
        best: Optional[_ModeBCandidate] = None

        for truck_id, truck in self.entity_mgr.trucks.items():
            seq = stops_by_truck.get(truck_id, [])
            start_pos = self._get_route_start_pos(truck, current_time, start_from_current_state)
            return_to_depot = True
            base_len = max(
                1.0,
                self._sequence_distance(
                    truck,
                    seq,
                    current_time,
                    start_from_current_state,
                    return_to_depot,
                ),
            )

            # 候选站点：任务点最近2个 + 未来路线插入最友好2个（去重后最多4个）。
            launch_options = self._select_b_wait_station_candidates(
                order=order,
                truck=truck,
                seq=seq,
                current_time=current_time,
                start_from_current_state=start_from_current_state,
                return_to_depot=return_to_depot,
                stations=stations,
            )

            # 使用候选站点评估 detour；launch_delay 用“从当前时刻到该站估计到达时刻”。
            for option in launch_options:
                diag.total_station_trials += 1
                launch_idx = option.launch_insert_idx
                launch_extra = option.launch_extra
                launch_delay = option.launch_eta

                scenario = self._evaluate_charging_station_departure(
                    drone=drone,
                    truck_tail_loc=start_pos,
                    launch_loc=option.station_pos,
                    launch_station_id=option.station_id,
                    truck_distance_to_launch=option.launch_path_distance,
                    launch_delay=launch_delay,
                    delivery_loc=order.delivery_loc,
                    payload=order.payload_weight,
                    recovery_pool=recovery_pool,
                    current_time=current_time,
                    order=order,
                )
                if not scenario or not scenario.get("feasible", False):
                    continue
                diag.scenario_feasible_trials += 1

                recovery_id = str(scenario["recovery_station_id"])
                recovery_insert_idx = launch_idx
                recovery_extra = 0.0
                if recovery_id in self.entity_mgr.stations and recovery_id != option.station_id:
                    recovery_insert_idx, recovery_extra = self._best_station_insertion(
                        truck,
                        seq,
                        self.entity_mgr.stations[recovery_id].location,
                        current_time,
                        start_from_current_state,
                        return_to_depot,
                    )

                extra_total = launch_extra + recovery_extra
                detour_ratio = extra_total / base_len

                recovery_entity = (
                    self.entity_mgr.stations.get(recovery_id)
                    or self.entity_mgr.depots.get(recovery_id)
                )
                recovery_pos = recovery_entity.location if recovery_entity is not None else option.station_pos
                scenario_truck_increment = self._estimate_truck_increment_for_drone_support(
                    start_pos,
                    order.delivery_loc,
                    option.station_pos,
                    recovery_pos,
                )
                scenario_truck_increment = max(0.0, scenario_truck_increment)
                scenario_uav_cost_dist = max(
                    0.0,
                    float(scenario.get("cost_dist", 0.0))
                    - self.C_DIST_ET * scenario_truck_increment,
                )
                scenario_uav_cost_energy = max(
                    0.0,
                    float(scenario.get("cost_energy", 0.0))
                    - self.C_ENERGY_ET * self._truck_energy_wh(scenario_truck_increment),
                )

                waiting_penalty = self.WAIT_PENALTY_FACTOR * max(
                    0.0,
                    float(scenario.get("wait_duration", 0.0)),
                ) / max(1.0, self.WAIT_PENALTY_TIME_SCALE_S)
                delta_dist = scenario_uav_cost_dist + self.C_DIST_ET * extra_total
                delta_energy = scenario_uav_cost_energy + self.C_ENERGY_ET * self._truck_energy_wh(extra_total)
                delta_penalty = float(scenario.get("cost_penalty", 0.0)) + waiting_penalty
                delta_score = delta_dist + delta_energy + delta_penalty

                if delta_score < diag.best_raw_score:
                    diag.best_raw_score = delta_score
                    diag.best_raw_detour_ratio = detour_ratio
                    diag.best_raw_cost_dist = delta_dist
                    diag.best_raw_cost_energy = delta_energy
                    diag.best_raw_cost_penalty = delta_penalty

                # 关键约束 1：只有当 B 优于 A 才允许进入候选。
                if detour_ratio > self.B_DETOUR_RATIO_LIMIT:
                    diag.rejected_detour += 1
                    continue
                if not math.isfinite(a_baseline) or delta_score >= a_baseline:
                    diag.rejected_score += 1
                    continue

                cand = _ModeBCandidate(
                    truck_id=truck_id,
                    drone_id=drone.drone_id,
                    launch_station_id=option.station_id,
                    launch_insert_idx=launch_idx,
                    recovery_station_id=recovery_id,
                    recovery_insert_idx=recovery_insert_idx,
                    launch_time=float(scenario.get("launch_time", current_time + launch_delay)),
                    wait_duration=float(scenario.get("wait_duration", 0.0)),
                    delta_score=delta_score,
                    cost_dist=delta_dist,
                    cost_energy=delta_energy,
                    cost_penalty=delta_penalty,
                    distance=float(scenario.get("distance", 0.0)),
                    detour_ratio=detour_ratio,
                )
                if best is None or cand.delta_score < best.delta_score:
                    best = cand
                diag.accepted_trials += 1

        return best, diag

    def _select_b_wait_station_candidates(
        self,
        order: "Order",
        truck: "Truck",
        seq: list[dict],
        current_time: float,
        start_from_current_state: bool,
        return_to_depot: bool,
        stations: list,
    ) -> list[_StationLaunchOption]:
        """选择 B_WAIT 出发站候选：路线增量优先 + 最早可放飞 + 任务近邻并集。"""
        if not stations:
            return []

        speed = max(1e-6, float(getattr(truck, "speed", 0.0)))
        route_ranked: list[tuple[float, float, float, _StationLaunchOption]] = []
        eta_ranked: list[tuple[float, float, float, _StationLaunchOption]] = []
        task_ranked: list[tuple[float, float, float, _StationLaunchOption]] = []

        for station in stations:
            launch_idx, launch_extra = self._best_station_insertion(
                truck,
                seq,
                station.location,
                current_time,
                start_from_current_state,
                return_to_depot,
            )
            launch_path_distance = self._distance_to_inserted_station(
                truck,
                seq,
                launch_idx,
                station.location,
                current_time,
                start_from_current_state,
            )
            launch_eta = launch_path_distance / speed
            task_dist = self._dist(order.delivery_loc, station.location)
            option = _StationLaunchOption(
                station_id=station.station_id,
                station_pos=station.location,
                launch_insert_idx=launch_idx,
                launch_extra=max(0.0, launch_extra),
                launch_path_distance=max(0.0, launch_path_distance),
                launch_eta=max(0.0, launch_eta),
                task_distance=task_dist,
                source="",
            )
            route_ranked.append((option.launch_extra, option.launch_eta, option.task_distance, option))
            eta_ranked.append((option.launch_eta, option.launch_extra, option.task_distance, option))
            task_ranked.append((option.task_distance, option.launch_eta, option.launch_extra, option))

        # 未来路线友好：按插入增量优先，ETA 与任务距离作为次级排序。
        route_ranked.sort(key=lambda x: (x[0], x[1], x[2]))
        route_top: list[_StationLaunchOption] = []
        for _, _, _, opt in route_ranked[: self.B_WAIT_ROUTE_TOP_K]:
            route_top.append(
                _StationLaunchOption(
                    station_id=opt.station_id,
                    station_pos=opt.station_pos,
                    launch_insert_idx=opt.launch_insert_idx,
                    launch_extra=opt.launch_extra,
                    launch_path_distance=opt.launch_path_distance,
                    launch_eta=opt.launch_eta,
                    task_distance=opt.task_distance,
                    source=f"route_top{self.B_WAIT_ROUTE_TOP_K}",
                )
            )

        # 最早可放飞：优先 ETA，补偿“路线上很顺但很晚才经过”的候选。
        eta_ranked.sort(key=lambda x: (x[0], x[1], x[2]))
        eta_top: list[_StationLaunchOption] = []
        for _, _, _, opt in eta_ranked[: self.B_WAIT_ETA_TOP_K]:
            eta_top.append(
                _StationLaunchOption(
                    station_id=opt.station_id,
                    station_pos=opt.station_pos,
                    launch_insert_idx=opt.launch_insert_idx,
                    launch_extra=opt.launch_extra,
                    launch_path_distance=opt.launch_path_distance,
                    launch_eta=opt.launch_eta,
                    task_distance=opt.task_distance,
                    source=f"eta_top{self.B_WAIT_ETA_TOP_K}",
                )
            )

        # 任务点近邻：按任务距离排序。
        task_ranked.sort(key=lambda x: (x[0], x[1], x[2]))
        task_top: list[_StationLaunchOption] = []
        for _, _, _, opt in task_ranked[: self.B_WAIT_TASK_TOP_K]:
            task_top.append(
                _StationLaunchOption(
                    station_id=opt.station_id,
                    station_pos=opt.station_pos,
                    launch_insert_idx=opt.launch_insert_idx,
                    launch_extra=opt.launch_extra,
                    launch_path_distance=opt.launch_path_distance,
                    launch_eta=opt.launch_eta,
                    task_distance=opt.task_distance,
                    source=f"task_top{self.B_WAIT_TASK_TOP_K}",
                )
            )

        # 去重合并：优先路线增量，再补最早放飞与任务近邻。
        merged: list[_StationLaunchOption] = []
        seen: set[str] = set()
        for opt in route_top + eta_top + task_top:
            if opt.station_id in seen:
                continue
            seen.add(opt.station_id)
            merged.append(opt)
            if len(merged) >= self.B_WAIT_MAX_CANDIDATES:
                break
        return merged

    def _distance_to_inserted_station(
        self,
        truck: "Truck",
        seq: list[dict],
        insert_idx: int,
        station_pos: Position3D,
        current_time: float,
        start_from_current_state: bool,
    ) -> float:
        """估计从当前起点到“在 insert_idx 处插入站点”时的到站里程。"""
        prefix_len = self._prefix_distance(
            truck,
            seq,
            insert_idx,
            current_time,
            start_from_current_state,
        )
        start_pos = self._get_route_start_pos(truck, current_time, start_from_current_state)
        prev_pos = start_pos if insert_idx == 0 else seq[insert_idx - 1]["position"]
        return max(0.0, prefix_len + self._road_dist(prev_pos, station_pos))

    def _ensure_recovery_stop(
        self,
        stops: list[dict],
        station_id: str,
        station_pos: Position3D,
        suggested_idx: int,
        min_service: float,
    ) -> int:
        """确保序列中存在 recovery 站点，存在则提升服务时长，不存在则插入。"""
        for idx, stop in enumerate(stops):
            if stop.get("node_id") != station_id:
                continue
            stop["node_type"] = "recovery"
            stop["position"] = station_pos
            stop["service_time"] = max(float(stop.get("service_time", 0.0)), float(min_service))
            return idx

        idx = max(0, min(suggested_idx, len(stops)))
        stops.insert(
            idx,
            {
                "node_id": station_id,
                "node_type": "recovery",
                "position": station_pos,
                "order_id": "",
                "service_time": max(0.0, float(min_service)),
            },
        )
        return idx

    def _prefix_distance(
        self,
        truck: "Truck",
        seq: list[dict],
        stop_count: int,
        current_time: float,
        start_from_current_state: bool,
    ) -> float:
        """从起点到前 stop_count 个停靠的累计里程。"""
        cur = self._get_route_start_pos(truck, current_time, start_from_current_state)
        total = 0.0
        for stop in seq[: max(0, min(stop_count, len(seq)))]:
            total += self._road_dist(cur, stop["position"])
            cur = stop["position"]
        return total

    def _augment_backbone_stations(
        self,
        truck_id: str,
        stops: list[dict],
        current_time: float,
        start_from_current_state: bool,
        return_to_depot: bool,
    ) -> int:
        """阶段 1：沿骨干路径补充可达充电站。"""
        truck = self.entity_mgr.trucks.get(truck_id)
        if truck is None:
            return 0

        stations = list(self.entity_mgr.stations.values())
        if not stations:
            return 0

        drone_pool = [d for d in self.entity_mgr.drones.values() if d.battery_current > self.min_reserve_energy]
        if not drone_pool:
            return 0

        inserted = 0
        idx = 0
        # 仅在“中间路段”插站，不在最后回仓段前强行加站。
        while idx < len(stops):
            start_pos = self._get_route_start_pos(truck, current_time, start_from_current_state)
            prev_pos = start_pos if idx == 0 else stops[idx - 1]["position"]

            if idx < len(stops):
                next_pos = stops[idx]["position"]
                direct = self._road_dist(prev_pos, next_pos)
            else:
                break

            if direct <= 1e-6:
                idx += 1
                continue

            best_station = None
            best_extra = float("inf")
            for station in stations:
                if any(s.get("node_id") == station.station_id for s in stops):
                    continue
                detour = self._road_dist(prev_pos, station.location) + self._road_dist(station.location, next_pos)
                extra = detour - direct
                ratio = extra / max(1.0, direct)
                if ratio > self.BACKBONE_STATION_DETOUR_LIMIT or extra > self.BACKBONE_STATION_MAX_EXTRA_M:
                    continue
                if not self._is_backbone_station_energy_feasible(drone_pool, prev_pos, next_pos, station.location):
                    continue
                if extra < best_extra:
                    best_extra = extra
                    best_station = station

            if best_station is None:
                idx += 1
                continue

            stops.insert(
                idx,
                {
                    "node_id": best_station.station_id,
                    "node_type": "station",
                    "position": best_station.location,
                    "order_id": "",
                    "service_time": 0.0,
                },
            )
            inserted += 1
            idx += 2

        return inserted

    def _prune_nonessential_station_stops(
        self,
        stops_by_truck: dict[str, list[dict]],
        allocations: list[AllocationResult],
    ) -> None:
        """移除当前批次无任务需求的 station 停靠，减少无效绕行/停留。"""
        required_by_truck: dict[str, set[str]] = {
            tid: set() for tid in stops_by_truck
        }

        for alloc in allocations:
            if not alloc.feasible:
                continue
            if alloc.mode not in ("B", "B_WAIT"):
                continue
            truck_id = alloc.vehicle_id
            if truck_id not in required_by_truck:
                continue
            if alloc.launch_station_id:
                required_by_truck[truck_id].add(alloc.launch_station_id)
            if alloc.recovery_station_id and alloc.recovery_station_id in self.entity_mgr.stations:
                required_by_truck[truck_id].add(alloc.recovery_station_id)

        for truck_id, seq in stops_by_truck.items():
            required = required_by_truck.get(truck_id, set())
            if not seq:
                continue

            kept: list[dict] = []
            removed = 0
            for stop in seq:
                if stop.get("node_type") != "station":
                    kept.append(stop)
                    continue

                sid = str(stop.get("node_id", ""))
                if stop is seq[-1]:
                    # 保留终点锚站，保证“最终停靠最近充电站”语义在后续增量中延续。
                    kept.append(stop)
                    continue
                if sid and sid in required:
                    # 保留被任务引用的站点，并升级为 recovery 节点，便于时序一致处理。
                    stop["node_type"] = "recovery"
                    kept.append(stop)
                else:
                    removed += 1

            if removed > 0:
                logger.info(
                    "[MMCE-BI] 卡车 %s 移除 %d 个非任务必需站点停靠",
                    truck_id,
                    removed,
                )
            stops_by_truck[truck_id] = kept

    def _ensure_terminal_station_stop(self, stops: list[dict]) -> bool:
        """确保路线最后停靠为“最后任务点最近充电站”。"""
        if not stops:
            return False

        last = stops[-1]
        anchor = self._nearest_station_for_pos(last["position"])
        if anchor is None:
            return False

        station_id, station_pos = anchor
        if (
            str(last.get("node_id", "")) == station_id
            and str(last.get("node_type", "")) in {"station", "recovery"}
        ):
            return False

        stops.append(
            {
                "node_id": station_id,
                "node_type": "station",
                "position": station_pos,
                "order_id": "",
                "service_time": 0.0,
            }
        )
        logger.info(
            "[MMCE-BI] 终点锚定充电站: station=%s（相对最后任务点）",
            station_id,
        )
        return True

    def _is_backbone_station_energy_feasible(
        self,
        drones: list["Drone"],
        pos_i: Position3D,
        pos_j: Position3D,
        station_pos: Position3D,
    ) -> bool:
        """校验 E >= i->s->i 或 i->s->j（至少一个可行）。"""
        for drone in drones:
            e_isi = (
                self._flight_energy(drone, pos_i, station_pos, 0.0)
                + self._flight_energy(drone, station_pos, pos_i, 0.0)
            ) * self.ENERGY_SAFETY_FACTOR
            e_isj = (
                self._flight_energy(drone, pos_i, station_pos, 0.0)
                + self._flight_energy(drone, station_pos, pos_j, 0.0)
            ) * self.ENERGY_SAFETY_FACTOR
            if min(e_isi, e_isj) <= float(drone.battery_current):
                return True
        return False
