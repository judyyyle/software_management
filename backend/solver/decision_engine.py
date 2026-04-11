#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 调度决策编排层

职责：
  1. 触发求解算法（贪心、ALNS、DRL）并获得分配方案
  2. 将分配方案应用到实体和订单状态
  3. 记录调度日志供前端展示

编排流程：
  [OrderManager.pending_orders] → [GreedyBaseline.dispatch()]
    → [DispatchDecisionEngine.execute_plan()]
    → [更新订单状态 + 触发实体行为]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config.loader import load_solver_energy_params
from solver.factory import create_solver, list_solvers
from solver.greedy_baseline import AllocationResult, DispatchPlan
from solver.interfaces import DispatchSolver
from core.entities.primitives import RouteWaypoint, WaypointAction, SourceType, DroneStatus

if TYPE_CHECKING:
    from entity_manager import EntityManager
    from order_manager import OrderManager

logger = logging.getLogger(__name__)


class DispatchDecisionEngine:
    """
    调度决策编排器。

    职责：
        1. 调用已配置求解器（Greedy/ALNS/DRL 等）进行分配
      2. 应用分配结果到实体和订单的状态机
      3. 发出后续行为触发（如驾驶、飞行、充电）
    """

    def __init__(
        self,
        entity_mgr: "EntityManager",
        order_mgr: "OrderManager",
        solver: DispatchSolver | None = None,
        solver_name: str = "greedy",
    ) -> None:
        """
        Args:
            entity_mgr: EntityManager 实例
            order_mgr:  OrderManager 实例
            solver:     可选，外部注入求解器实例
            solver_name:未注入 solver 时，按名称从工厂创建（默认 greedy）
        """
        self.entity_mgr = entity_mgr
        self.order_mgr = order_mgr
        if solver is not None:
            self.solver = solver
            self.solver_name = solver.__class__.__name__.lower()
        else:
            self.solver_name = solver_name.strip().lower()
            self.solver = create_solver(self.solver_name, self.entity_mgr)

        runtime_cfg = load_solver_energy_params()
        self.TRUCK_DRONE_LAUNCH_TIME = runtime_cfg.truck_drone_launch_time_s
        self.TRUCK_DRONE_RECOVER_TIME = runtime_cfg.truck_drone_recover_time_s

    def set_solver(self, solver_name: str) -> None:
        """按名称切换求解器实例。"""
        target = solver_name.strip().lower()
        if not target:
            raise ValueError("solver_name 不能为空")
        if target == self.solver_name:
            return
        self.solver = create_solver(target, self.entity_mgr)
        self.solver_name = target
        logger.info("[DispatchDecisionEngine] 已切换求解器为 %s", target)

    @staticmethod
    def get_available_solvers() -> list[str]:
        """返回当前可用求解器列表。"""
        return list_solvers()

    def execute(self, current_time: float, bbox: dict, scene_id: str | None = None) -> DispatchPlan:
        """
        执行一轮调度决策。

        流程：
          1. 从 order_mgr.pending_orders 取快照
          2. 调用贪心求解器
          3. 应用分配方案（更新订单状态）

        Args:
            current_time: 仿真时刻（秒）
            bbox: 地图边界 {"minx": float, "miny": float, "maxx": float, "maxy": float}
            scene_id: 预设场景 ID（可选，如 'default_test_4x4km'）

        Returns:
            分配方案（DispatchPlan）
        """
        pending = dict(self.order_mgr.pending_orders)  # 快照
        if not pending:
            logger.debug("[DispatchDecisionEngine] 无待分配订单")
            return DispatchPlan(
                allocations=[],
                cost_total=0.0,
                summary={"total_orders": 0, "feasible": 0, "modes": {}},
            )

        # 调用求解器，传递 scene_id 以支持使用缓存的 OSM 数据
        plan = self.solver.dispatch(pending, current_time, bbox, scene_id=scene_id)

        # 应用分配（更新订单状态）
        self._apply_plan(plan, current_time)

        # 构建无人机路由信息用于前端展示
        self._build_drone_routes(plan, current_time)

        # 输出卡车路径摘要
        for truck_id, route in plan.truck_routes.items():
            logger.info(
                "[DispatchDecisionEngine] 卡车 %s 路径 %d 节点，里程 %.0fm，"
                "经停充电站 %s",
                truck_id,
                len(route.nodes),
                route.total_distance,
                route.charging_stop_ids or "（无）",
            )
            # 详细路线打印
            self._log_detailed_route(route, plan.allocations)

        return plan

    def execute_incremental(
        self,
        new_orders: dict,
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """增量调度：仅对动态新单执行拍卖，尊重已有契约。

        核心原则：
          1. 状态冻结 — 不覆盖飞行中无人机的位置/路径
          2. 路径拼接 — 新任务段追加到已有路径末尾
          3. 卡车路由保护 — 不重写正在执行的卡车路由
        """
        if not new_orders:
            logger.debug("[DispatchDecisionEngine] 无新增订单")
            return DispatchPlan(
                allocations=[],
                cost_total=0.0,
                summary={"total_orders": 0, "feasible": 0, "modes": {}},
            )

        if not hasattr(self.solver, "dispatch_incremental"):
            logger.info("[DispatchDecisionEngine] 当前求解器不支持增量调度，回退到全量")
            return self.execute(current_time, bbox, scene_id)

        plan = self.solver.dispatch_incremental(new_orders, current_time, bbox, scene_id=scene_id)
        self._apply_plan(plan, current_time, incremental=True)
        self._build_drone_routes(plan, current_time)

        for truck_id, route in plan.truck_routes.items():
            logger.info(
                "[DispatchDecisionEngine] 增量调度卡车 %s 路径 %d 节点，里程 %.0fm",
                truck_id, len(route.nodes), route.total_distance,
            )

        return plan

    def try_fulfill_contracts(self, current_time: float) -> None:
        """尝试兑现已完成回收的契约。

        遍历求解器的活跃契约，若无人机已不再飞行（IDLE/CHARGING）
        且当前时刻已超过 uav_arrival_time，则标记契约为 fulfilled。
        供仿真引擎在每个 tick 中调用。
        """
        if not hasattr(self.solver, "fulfill_contract"):
            return
        if not hasattr(self.solver, "_active_contracts"):
            return

        for contract in self.solver._active_contracts:
            if contract.status != "active":
                continue
            drone = self.entity_mgr.drones.get(contract.drone_id)
            if drone is None:
                continue
            if current_time >= contract.uav_arrival_time and not drone.status.is_flying():
                self.solver.fulfill_contract(contract.contract_id)

    def _apply_plan(self, plan: DispatchPlan, current_time: float, incremental: bool = False) -> None:
        """
        应用分配方案，更新订单和实体状态。

        Args:
            incremental: 增量模式。为 True 时保护正在执行中的卡车路由，
                         仅将新停靠事件追加到已有的时刻表中，不覆盖卡车物理路线。
        """
        for alloc in plan.allocations:
            order = self.order_mgr.pending_orders.get(alloc.order_id)
            if not order:
                continue

            if alloc.feasible:
                # 将订单转入 assigned
                self.order_mgr.pending_orders.pop(alloc.order_id, None)
                self.order_mgr.assigned_orders[alloc.order_id] = order

                # 更新订单的分配信息
                order.assigned_vehicle_id = alloc.vehicle_id
                order.assigned_mode = alloc.mode
                
                # 更新订单状态：PENDING → ASSIGNED
                from core.entities.primitives import TaskStatus
                order.update_status(TaskStatus.ASSIGNED)

                if alloc.mode in ("B_WAIT", "B_DYNAMIC"):
                    logger.info(
                        "[DispatchDecisionEngine] 分配 %s 至 %s（模式 %s, 出发站 %s, 距离 %.1fm, 回收点 %s），状态转为 ASSIGNED",
                        alloc.order_id,
                        alloc.vehicle_id,
                        alloc.mode,
                        alloc.launch_station_id or "-",
                        alloc.distance,
                        alloc.recovery_station_id or "-",
                    )
                else:
                    logger.info(
                        "[DispatchDecisionEngine] 分配 %s 至 %s（模式 %s, 距离 %.1fm, 回收点 %s），状态转为 ASSIGNED",
                        alloc.order_id,
                        alloc.vehicle_id,
                        alloc.mode,
                        alloc.distance,
                        alloc.recovery_station_id or "-",
                    )
            else:
                # 不可行，保持 pending
                logger.warning(
                    "[DispatchDecisionEngine] 分配失败 %s: %s",
                    alloc.order_id,
                    alloc.reason,
                )

        # ── 第二步：应用卡车路由 ────────────────────────────────────────────
        for truck_id, route in plan.truck_routes.items():
            truck = self.entity_mgr.trucks.get(truck_id)
            if truck is None:
                logger.warning("[DispatchDecisionEngine] 卡车 %s 不存在", truck_id)
                continue

            self._recalculate_truck_route_timing_for_b_wait(route, plan.allocations, current_time)

            if incremental:
                # 增量模式：保护正在执行的卡车路由，只追加新的停靠事件
                self._merge_incremental_truck_stops(truck, route, plan.allocations, current_time)
                continue

            route_nodes = [node.node_id for node in route.nodes]
            route_positions = [node.position for node in route.nodes]

            try:
                truck.set_route(route_nodes, route_positions, current_time, geometry=route.geometry)
                truck._planned_route_stops = [
                    {
                        "node_id": node.node_id,
                        "node_type": node.node_type,
                        "position": node.position,
                        "arrival_time": node.arrival_time,
                        "departure_time": node.departure_time,
                        "order_id": node.order_id,
                    }
                    for node in route.nodes
                ]
                truck._planned_route_cursor = 0
                logger.info(
                    "[DispatchDecisionEngine] 已将路由应用至卡车 %s（%d 个关键节点，几何路径 %d 点）",
                    truck_id, len(route_nodes), len(route.geometry),
                )
            except Exception as e:
                logger.exception(
                    "[DispatchDecisionEngine] 为卡车 %s 应用路由失败: %s",
                    truck_id, str(e),
                )

        # ── 补齐 B_WAIT/B_DYNAMIC 起飞时刻：使用卡车实际到达出发站时刻 ─────────
        for alloc in plan.allocations:
            if not alloc.feasible or alloc.mode not in ("B_WAIT", "B_DYNAMIC") or not alloc.launch_station_id:
                continue
            truck_route = plan.truck_routes.get(alloc.vehicle_id)
            if truck_route is None:
                continue

            launch_node = next(
                (n for n in truck_route.nodes if n.node_id == alloc.launch_station_id),
                None,
            )
            if launch_node is not None:
                alloc.launch_time = launch_node.arrival_time + self.TRUCK_DRONE_LAUNCH_TIME

        # ── 第三步：应用无人机路由 ────────────────────────────────────────────
        self._setup_drone_routes(plan, current_time)

    def _merge_incremental_truck_stops(
        self,
        truck,
        new_route: "TruckRoute",
        allocations: list,
        current_time: float,
    ) -> None:
        """增量模式下将新的停靠事件追加到卡车已有时刻表中，不覆盖物理路线。

        原则：
          - 保留已执行和正在执行的停靠事件（cursor 之前）
          - 将新的 recovery / customer 停靠按时间顺序插入未执行区间
          - 不调用 truck.set_route()，卡车继续沿原有几何路径行驶
        """
        existing_stops: list[dict] = getattr(truck, "_planned_route_stops", None) or []
        cursor = int(getattr(truck, "_planned_route_cursor", 0))

        if not existing_stops:
            # 卡车尚未有路线（首次被增量调度命中），按全量模式设置
            route_nodes = [node.node_id for node in new_route.nodes]
            route_positions = [node.position for node in new_route.nodes]
            try:
                truck.set_route(route_nodes, route_positions, current_time, geometry=new_route.geometry)
                truck._planned_route_stops = [
                    {
                        "node_id": n.node_id, "node_type": n.node_type,
                        "position": n.position, "arrival_time": n.arrival_time,
                        "departure_time": n.departure_time, "order_id": n.order_id,
                    }
                    for n in new_route.nodes
                ]
                truck._planned_route_cursor = 0
                logger.info(
                    "[DispatchDecisionEngine] 增量模式首次为卡车 %s 设置路由 (%d 节点)",
                    truck.truck_id, len(route_nodes),
                )
            except Exception as e:
                logger.exception("[DispatchDecisionEngine] 增量首次路由设置失败 %s: %s", truck.truck_id, e)
            return

        # 提取新路由中需要追加的事件性节点（customer / recovery）
        existing_ids = {s.get("node_id") for s in existing_stops}
        new_stops = []
        for node in new_route.nodes:
            if node.node_type in ("customer", "recovery", "station"):
                if node.node_id in existing_ids:
                    continue
                new_stops.append({
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "position": node.position,
                    "arrival_time": node.arrival_time,
                    "departure_time": node.departure_time,
                    "order_id": node.order_id,
                })

        if not new_stops:
            logger.debug(
                "[DispatchDecisionEngine] 增量模式卡车 %s 无需追加新停靠",
                truck.truck_id,
            )
            return

        # 将新停靠按 arrival_time 插入到未执行区间
        future_stops = existing_stops[cursor:]
        merged = future_stops + new_stops
        merged.sort(key=lambda s: float(s.get("arrival_time", float("inf"))))
        truck._planned_route_stops = existing_stops[:cursor] + merged
        # cursor 不变，继续从之前的位置处理
        logger.info(
            "[DispatchDecisionEngine] 增量模式追加 %d 个停靠到卡车 %s "
            "(cursor=%d, 总计 %d 停靠)",
            len(new_stops), truck.truck_id, cursor, len(truck._planned_route_stops),
        )

    def _recalculate_truck_route_timing_for_b_wait(
        self,
        route: "TruckRoute",
        allocations: list["AllocationResult"],
        current_time: float,
    ) -> None:
        """
        按 B_WAIT 的起飞站和回收站关系重算卡车停靠时序。

        关键逻辑：
          - recovery 节点是否需要等待，取决于“无人机预计返航时刻”与“卡车到站时刻”的差
          - 无人机预计返航时刻 = launch_station_arrival + alloc.wait_duration
          - 若卡车到达 recovery 时无人机已返航，则 recovery 等待时间应为 0
        """
        if len(route.nodes) < 2:
            return

        related_allocs = [
            alloc for alloc in allocations
            if alloc.feasible
            and alloc.mode == "B_WAIT"
            and alloc.vehicle_id == route.truck_id
            and alloc.recovery_station_id
        ]
        if not related_allocs:
            return

        allocs_by_recovery: dict[str, list["AllocationResult"]] = {}
        allocs_by_launch: dict[str, list["AllocationResult"]] = {}
        for alloc in related_allocs:
            allocs_by_recovery.setdefault(alloc.recovery_station_id, []).append(alloc)
            if alloc.launch_station_id:
                allocs_by_launch.setdefault(alloc.launch_station_id, []).append(alloc)

        original_arrivals = [node.arrival_time for node in route.nodes]
        original_departures = [node.departure_time for node in route.nodes]
        base_services = [
            max(0.0, dep - arr)
            for arr, dep in zip(original_arrivals, original_departures)
        ]
        travel_deltas = [0.0]
        for i in range(1, len(route.nodes)):
            travel_deltas.append(max(0.0, original_arrivals[i] - original_departures[i - 1]))

        launch_arrivals: dict[str, float] = {}

        for i, node in enumerate(route.nodes):
            if i == 0:
                arrival = original_arrivals[0]
            else:
                arrival = route.nodes[i - 1].departure_time + travel_deltas[i]

            if node.node_type == "recovery":
                needed_departure = arrival
                for alloc in allocs_by_recovery.get(node.node_id, []):
                    launch_arrival = launch_arrivals.get(alloc.launch_station_id)
                    if launch_arrival is None:
                        launch_arrival = alloc.launch_time if alloc.launch_time > 0 else current_time
                    expected_recovery_time = launch_arrival + alloc.wait_duration
                    needed_departure = max(needed_departure, expected_recovery_time)
                service_time = max(0.0, needed_departure - arrival)
                launch_ops = allocs_by_launch.get(node.node_id, [])
                recovery_ops = allocs_by_recovery.get(node.node_id, [])
                op_hold = 0.0
                if launch_ops:
                    op_hold = max(op_hold, self.TRUCK_DRONE_LAUNCH_TIME)
                if recovery_ops:
                    op_hold = max(op_hold, self.TRUCK_DRONE_RECOVER_TIME)
                if op_hold > 0.0:
                    # 若同站同时有放飞和回收，按可并行处理取 max，而非累加。
                    service_time = max(service_time, op_hold)
            else:
                service_time = base_services[i]

            departure = arrival + service_time
            node.arrival_time = arrival
            node.departure_time = departure

            if node.node_id not in launch_arrivals:
                launch_arrivals[node.node_id] = arrival

    def _log_detailed_route(self, route: "TruckRoute", allocations: list["AllocationResult"]) -> None:
        """
        打印卡车的详细路线，包括每个节点的信息和关联的订单/无人机操作。
        """
        logger.info("[DispatchDecisionEngine] 卡车 %s 详细路线：", route.truck_id)
        for i, node in enumerate(route.nodes):
            action_desc = self._get_node_action_description(node, allocations)
            logger.info(
                "  [%d] %s (%s) - 到达: %.1fs, 离开: %.1fs | %s",
                i + 1,
                node.node_id,
                node.node_type,
                node.arrival_time,
                node.departure_time,
                action_desc,
            )

    def _get_node_action_description(self, node: "TruckRouteNode", allocations: list["AllocationResult"]) -> str:
        """
        根据节点类型和关联信息，生成动作描述。
        """
        if node.node_type == "depot":
            if "_return" in node.node_id:
                return "返回仓库"
            else:
                return "从仓库出发"
        elif node.node_type == "customer":
            return f"配送订单 {node.order_id}"
        elif node.node_type == "recovery":
            launch_orders = [
                alloc.order_id for alloc in allocations
                if alloc.feasible and alloc.mode in ("B_WAIT", "B_DYNAMIC") and alloc.launch_station_id == node.node_id
            ]
            recovery_orders = [
                alloc.order_id for alloc in allocations
                if alloc.feasible and alloc.mode in ("B", "B_WAIT") and alloc.recovery_station_id == node.node_id
            ]

            if launch_orders and recovery_orders:
                return (
                    f"放飞+回收无人机（放飞订单: {', '.join(launch_orders)}；"
                    f"回收订单: {', '.join(recovery_orders)}）"
                )
            if launch_orders:
                return f"放飞无人机（订单: {', '.join(launch_orders)}）"
            if recovery_orders:
                return f"回收无人机（订单: {', '.join(recovery_orders)}）"
            return "站点停靠"
        elif node.node_type == "station":
            return "经停充电站（广播给无人机）"
        else:
            return "未知动作"

    def _setup_drone_routes(self, plan: DispatchPlan, current_time: float) -> None:
        """
        为分配的无人机设置路由计划。

        增量安全：跳过正在飞行中的无人机，避免覆盖其当前路径和位置。
        """
        for alloc in plan.allocations:
            if not alloc.feasible or alloc.mode == "A" or not alloc.drone_id:
                continue

            drone = self.entity_mgr.drones.get(alloc.drone_id)
            if drone is None:
                logger.warning("[DispatchDecisionEngine] 无人机 %s 不存在", alloc.drone_id)
                continue

            # 状态冻结：正在飞行的无人机使用路径拼接（串联任务）
            is_relay = getattr(alloc, "_is_relay", False)
            if drone.status.is_flying and not is_relay:
                logger.warning(
                    "[DispatchDecisionEngine] 无人机 %s 正在飞行(status=%s)，"
                    "跳过路由设置以保护轨迹连续性",
                    alloc.drone_id, drone.status.value,
                )
                continue

            order = self.order_mgr.assigned_orders.get(alloc.order_id)
            if order is None:
                logger.warning("[DispatchDecisionEngine] 订单 %s 不存在", alloc.order_id)
                continue

            try:
                if alloc.mode in ("B", "B_WAIT", "B_DYNAMIC"):
                    truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
                    if truck is None:
                        logger.warning("[DispatchDecisionEngine] 卡车 %s 不存在", alloc.vehicle_id)
                        continue

                    if alloc.mode in ("B_WAIT", "B_DYNAMIC"):
                        launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
                        if launch_station is None:
                            logger.warning(
                                "[DispatchDecisionEngine] 充电站 %s 不存在（订单 %s）",
                                alloc.launch_station_id, alloc.order_id
                            )
                            continue
                        launch_loc = launch_station.location
                        wait_duration = alloc.wait_duration
                    else:
                        launch_loc = truck.get_location(current_time)
                        wait_duration = 0.0

                    delivery_loc = order.delivery_loc
                    recovery_id = alloc.recovery_station_id

                    if alloc.mode == "B_DYNAMIC":
                        recovery_entity = self.entity_mgr.depots.get(recovery_id)
                    else:
                        recovery_entity = self.entity_mgr.stations.get(recovery_id)
                    if recovery_entity is None:
                        logger.warning("[DispatchDecisionEngine] 回收点 %s 不存在", recovery_id)
                        continue

                    recovery_loc = recovery_entity.location

                    waypoints = [
                        RouteWaypoint(launch_loc, WaypointAction.PICKUP, alloc.order_id),
                        RouteWaypoint(delivery_loc, WaypointAction.DELIVER, alloc.order_id),
                        RouteWaypoint(recovery_loc, WaypointAction.DOCK_DEPOT, recovery_id),
                    ]
                    drone.set_route(waypoints)
                    try:
                        drone.assign_order(alloc.order_id, order.payload_weight)
                    except ValueError as e:
                        logger.warning("[DispatchDecisionEngine] 为无人机 %s 分配订单失败: %s", alloc.drone_id, e)
                    
                    from core.entities.primitives import TaskStatus
                    if order.status == TaskStatus.ASSIGNED:
                        order.update_status(TaskStatus.PICKED_UP)
                        order.update_status(TaskStatus.DELIVERING)
                    
                    if alloc.mode in ("B_WAIT", "B_DYNAMIC"):
                        drone.current_loc = truck.get_location(current_time)
                        drone.status = DroneStatus.IDLE
                        drone.transport_truck_id = truck.truck_id
                        drone.scheduled_launch_time = alloc.launch_time
                        drone.launch_station_id = alloc.launch_station_id
                        drone.waiting_recovery_station_id = ""
                        if alloc.drone_id not in truck.docked_drones:
                            truck.docked_drones.append(alloc.drone_id)
                        logger.info(
                            "[DispatchDecisionEngine] 为无人机 %s 设置路由（模式 %s）："
                            "随卡车 %s 运输至充电站 %s（t=%.1fs）后起飞，"
                            "飞行 %.1fs → 回收点 %s，订单: %s",
                            alloc.drone_id, alloc.mode, alloc.vehicle_id,
                            alloc.launch_station_id,
                            alloc.launch_time, wait_duration,
                            recovery_id, alloc.order_id,
                        )
                    else:
                        drone.transport_truck_id = None
                        drone.scheduled_launch_time = 0.0
                        drone.launch_station_id = ""
                        drone.waiting_recovery_station_id = ""
                        drone.status = DroneStatus.FLYING_TO_PICKUP
                        logger.info(
                            "[DispatchDecisionEngine] 为无人机 %s 设置路由（模式 B）："
                            "卡车位置 → 配送点 → 充电站 %s，订单: %s",
                            alloc.drone_id, recovery_id, alloc.order_id,
                        )

                elif alloc.mode == "C":
                    depot = self.entity_mgr.depots.get(alloc.vehicle_id)
                    if depot is None:
                        logger.warning("[DispatchDecisionEngine] 仓库 %s 不存在", alloc.vehicle_id)
                        continue

                    depot_loc = depot.location
                    delivery_loc = order.delivery_loc

                    if is_relay and drone.status.is_flying:
                        # 串联任务：拼接新路径到现有路径末尾
                        relay_origin = getattr(alloc, "_relay_origin", None) or delivery_loc
                        relay_waypoints = [
                            RouteWaypoint(relay_origin, WaypointAction.PICKUP, alloc.order_id),
                            RouteWaypoint(delivery_loc, WaypointAction.DELIVER, alloc.order_id),
                            RouteWaypoint(depot_loc, WaypointAction.DOCK_DEPOT, alloc.vehicle_id),
                        ]
                        drone.append_route(relay_waypoints)
                        logger.info(
                            "[DispatchDecisionEngine] 无人机 %s 串联任务：当前任务完成后 → "
                            "取货 → 配送点 → 仓库，新订单: %s",
                            alloc.drone_id, alloc.order_id,
                        )
                    else:
                        waypoints = [
                            RouteWaypoint(depot_loc, WaypointAction.PICKUP, alloc.order_id),
                            RouteWaypoint(delivery_loc, WaypointAction.DELIVER, alloc.order_id),
                            RouteWaypoint(depot_loc, WaypointAction.DOCK_DEPOT, alloc.vehicle_id),
                        ]
                        drone.set_route(waypoints)
                        drone.transport_truck_id = None
                        drone.scheduled_launch_time = 0.0
                        drone.launch_station_id = ""
                        drone.waiting_recovery_station_id = ""
                        drone.status = DroneStatus.FLYING_TO_PICKUP

                    try:
                        drone.assign_order(alloc.order_id, order.payload_weight)
                    except ValueError as e:
                        logger.warning("[DispatchDecisionEngine] 为无人机 %s 分配订单失败: %s", alloc.drone_id, e)

                    from core.entities.primitives import TaskStatus
                    if order.status == TaskStatus.ASSIGNED:
                        order.update_status(TaskStatus.PICKED_UP)
                        order.update_status(TaskStatus.DELIVERING)

                    if not is_relay:
                        logger.info(
                            "[DispatchDecisionEngine] 为无人机 %s 设置路由（模式 C）：仓库 → 配送点 → 仓库，订单: %s",
                            alloc.drone_id, alloc.order_id,
                        )

            except Exception as e:
                logger.exception(
                    "[DispatchDecisionEngine] 为无人机 %s 设置路由失败: %s",
                    alloc.drone_id, str(e),
                )

    def _build_drone_routes(self, plan: DispatchPlan, current_time: float) -> None:
        """
        为前端构建无人机路由信息（用于可视化展示）。

        将分配中的无人机路由转换为DroneRoute对象，包含完整的飞行路径坐标序列。
        """
        from solver.greedy_baseline import DroneRoute

        for alloc in plan.allocations:
            if not alloc.feasible or alloc.mode == "A" or not alloc.drone_id:
                continue

            order = self.order_mgr.assigned_orders.get(alloc.order_id)
            if order is None:
                continue

            try:
                if alloc.mode == "B":
                    # 模式 B：卡-空协同
                    truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
                    if truck is None:
                        continue
                    
                    launch_loc = truck.get_location(current_time)
                    delivery_loc = order.delivery_loc
                    recovery_id = alloc.recovery_station_id
                    recovery_station = self.entity_mgr.stations.get(recovery_id)
                    if recovery_station is None:
                        continue
                    recovery_loc = recovery_station.location

                    # 构建飞行路径：起点 → 配送点 → 回收点
                    path = [launch_loc, delivery_loc, recovery_loc]
                    # 转换为WGS84坐标供前端使用
                    path_wgs84 = []
                    for pos in path:
                        lon, lat = pos.to_wgs84()
                        path_wgs84.append([lon, lat])
                    
                    drone_route = DroneRoute(
                        drone_id=alloc.drone_id,
                        order_id=alloc.order_id,
                        path=[],  # 前端使用path_wgs84列表
                        mode="B",
                        launch_loc=launch_loc,
                        delivery_loc=delivery_loc,
                        recovery_loc=recovery_loc,
                    )
                    # 添加转换后的path
                    plan.drone_routes[alloc.drone_id] = drone_route

                elif alloc.mode in ("B_WAIT", "B_DYNAMIC"):
                    launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
                    if launch_station is None:
                        continue
                    
                    launch_loc = launch_station.location
                    delivery_loc = order.delivery_loc
                    recovery_id = alloc.recovery_station_id

                    if alloc.mode == "B_DYNAMIC":
                        recovery_entity = self.entity_mgr.depots.get(recovery_id)
                    else:
                        recovery_entity = self.entity_mgr.stations.get(recovery_id)
                    if recovery_entity is None:
                        continue
                    recovery_loc = recovery_entity.location

                    path = [launch_loc, delivery_loc, recovery_loc]
                    path_wgs84 = []
                    for pos in path:
                        lon, lat = pos.to_wgs84()
                        path_wgs84.append([lon, lat])
                    
                    drone_route = DroneRoute(
                        drone_id=alloc.drone_id,
                        order_id=alloc.order_id,
                        path=[],
                        mode=alloc.mode,
                        launch_loc=launch_loc,
                        delivery_loc=delivery_loc,
                        recovery_loc=recovery_loc,
                    )
                    plan.drone_routes[alloc.drone_id] = drone_route

                elif alloc.mode == "C":
                    # 模式 C：仓-空直递
                    depot = self.entity_mgr.depots.get(alloc.vehicle_id)
                    if depot is None:
                        continue
                    
                    depot_loc = depot.location
                    delivery_loc = order.delivery_loc

                    # 构建飞行路径：仓库 → 配送点 → 仓库
                    path = [depot_loc, delivery_loc, depot_loc]
                    # 转换为WGS84坐标供前端使用
                    path_wgs84 = []
                    for pos in path:
                        lon, lat = pos.to_wgs84()
                        path_wgs84.append([lon, lat])
                    
                    drone_route = DroneRoute(
                        drone_id=alloc.drone_id,
                        order_id=alloc.order_id,
                        path=[],  # 前端使用path_wgs84列表
                        mode="C",
                        launch_loc=depot_loc,
                        delivery_loc=delivery_loc,
                        recovery_loc=depot_loc,
                    )
                    plan.drone_routes[alloc.drone_id] = drone_route

            except Exception as e:
                logger.exception(
                    "[DispatchDecisionEngine._build_drone_routes] 构建无人机 %s 路由失败: %s",
                    alloc.drone_id, str(e),
                )
