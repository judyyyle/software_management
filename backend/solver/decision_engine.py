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

from solver.greedy_baseline import GreedyBaseline, AllocationResult, DispatchPlan
from core.entities.primitives import RouteWaypoint, WaypointAction, SourceType, DroneStatus

if TYPE_CHECKING:
    from entity_manager import EntityManager
    from order_manager import OrderManager

logger = logging.getLogger(__name__)


class DispatchDecisionEngine:
    """
    调度决策编排器。

    职责：
      1. 调用 GreedyBaseline（或后续的其他求解器）进行分配
      2. 应用分配结果到实体和订单的状态机
      3. 发出后续行为触发（如驾驶、飞行、充电）
    """

    def __init__(
        self,
        entity_mgr: "EntityManager",
        order_mgr: "OrderManager",
    ) -> None:
        """
        Args:
            entity_mgr: EntityManager 实例
            order_mgr:  OrderManager 实例
        """
        self.entity_mgr = entity_mgr
        self.order_mgr = order_mgr
        self.solver = GreedyBaseline(entity_mgr)

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

    def _apply_plan(self, plan: DispatchPlan, current_time: float) -> None:
        """
        应用分配方案，更新订单和实体状态。

        流程：
          1. 更新订单状态（pending → assigned）
          2. 应用卡车路由（触发卡车开始行驶）
          3. 无人机状态转换（标记为已分配）

        对每个分配结果：
          - 可行方案：订单转入 assigned，绑定 vehicle_id + mode
          - 不可行：订单保留 pending，等待下次调度机会
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

                # 更新日志，为 B_WAIT 模式增加出发站信息
                if alloc.mode == "B_WAIT":
                    logger.info(
                        "[DispatchDecisionEngine] 分配 %s 至 %s（模式 B_WAIT, 出发站 %s, 距离 %.1fm, 回收点 %s），状态转为 ASSIGNED",
                        alloc.order_id,
                        alloc.vehicle_id,
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

            # 在应用路由前，按 B_WAIT 任务关系重算节点时间：
            # recovery 节点仅在无人机尚未返航时才需要等待，避免无意义超长停留。
            self._recalculate_truck_route_timing_for_b_wait(route, plan.allocations, current_time)

            # 从路由节点中提取路网节点 ID 和位置
            route_nodes = [node.node_id for node in route.nodes]
            route_positions = [node.position for node in route.nodes]

            try:
                # 使用完整的几何路径（包含所有中间OSM节点）使卡车沿着真实道路行驶
                truck.set_route(route_nodes, route_positions, current_time, geometry=route.geometry)
                # 记录带时间戳的关键节点计划，供 EntityManager 在 tick 中驱动事件：
                # customer 节点触发订单完成，recovery 节点触发无人机回收上车。
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

        # ── 补齐 B_WAIT 起飞时刻：使用卡车实际到达出发站时刻 ───────────────────
        for alloc in plan.allocations:
            if not alloc.feasible or alloc.mode != "B_WAIT" or not alloc.launch_station_id:
                continue
            truck_route = plan.truck_routes.get(alloc.vehicle_id)
            if truck_route is None:
                continue

            launch_node = next(
                (n for n in truck_route.nodes if n.node_id == alloc.launch_station_id),
                None,
            )
            if launch_node is not None:
                alloc.launch_time = launch_node.arrival_time

        # ── 第三步：应用无人机路由 ────────────────────────────────────────────
        self._setup_drone_routes(plan, current_time)

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
        for alloc in related_allocs:
            allocs_by_recovery.setdefault(alloc.recovery_station_id, []).append(alloc)

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
            # 找到关联的订单
            related_orders = [
                alloc.order_id for alloc in allocations
                if alloc.recovery_station_id == node.node_id and alloc.mode == "B"
            ]
            if related_orders:
                return f"回收无人机（订单: {', '.join(related_orders)}）"
            else:
                return "回收无人机"
        elif node.node_type == "station":
            return "经停充电站（广播给无人机）"
        else:
            return "未知动作"

    def _setup_drone_routes(self, plan: DispatchPlan, current_time: float) -> None:
        """
        为分配的无人机设置路由计划。

        调度结果中的每个可行分配都包含：
          - drone_id: 分配的无人机 ID（仅模式 B/C 有效）
          - mode: 分配模式（'A'=卡车直递，'B'=卡-空协同，'C'=仓-空直递）
          - vehicle_id: 母体 ID（卡车 或 仓库）
          - delivery_loc: 配送目标点坐标
          - recovery_station_id: 回收点 ID（模式 B/C）

        路由规划：
          模式 B：[launch_loc (卡车当前)] → [delivery_loc] → [recovery_station]
          模式 C：[depot_loc] → [delivery_loc] → [depot_loc]
        """
        for alloc in plan.allocations:
            if not alloc.feasible or alloc.mode == "A" or not alloc.drone_id:
                # 模式 A 不涉及无人机；不可行分配忽略
                continue

            drone = self.entity_mgr.drones.get(alloc.drone_id)
            if drone is None:
                logger.warning("[DispatchDecisionEngine] 无人机 %s 不存在", alloc.drone_id)
                continue

            order = self.order_mgr.assigned_orders.get(alloc.order_id)
            if order is None:
                logger.warning("[DispatchDecisionEngine] 订单 %s 不存在", alloc.order_id)
                continue

            try:
                if alloc.mode == "B" or alloc.mode == "B_WAIT":
                    # 模式 B：卡-空协同（从卡车当前位置）
                    # 模式 B_WAIT：无人机在充电站等待后起飞
                    truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
                    if truck is None:
                        logger.warning("[DispatchDecisionEngine] 卡车 %s 不存在", alloc.vehicle_id)
                        continue

                    # 确定起飞位置
                    if alloc.mode == "B_WAIT":
                        # 从指定的充电站起飞
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
                        # 从卡车当前位置起飞
                        launch_loc = truck.get_location(current_time)
                        wait_duration = 0.0

                    delivery_loc = order.delivery_loc
                    recovery_id = alloc.recovery_station_id

                    recovery_station = self.entity_mgr.stations.get(recovery_id)
                    if recovery_station is None:
                        logger.warning("[DispatchDecisionEngine] 充电站 %s 不存在", recovery_id)
                        continue

                    recovery_loc = recovery_station.location

                    waypoints = [
                        RouteWaypoint(launch_loc, WaypointAction.PICKUP, alloc.order_id),
                        RouteWaypoint(delivery_loc, WaypointAction.DELIVER, alloc.order_id),
                        RouteWaypoint(recovery_loc, WaypointAction.DOCK_DEPOT, recovery_id),
                    ]
                    drone.set_route(waypoints)
                    # 为无人机分配订单（在航路的 PICKUP 点会绑定）
                    try:
                        drone.assign_order(alloc.order_id, order.payload_weight)
                    except ValueError as e:
                        logger.warning("[DispatchDecisionEngine] 为无人机 %s 分配订单失败: %s", alloc.drone_id, e)
                    
                    # 订单状态转为 PICKED_UP → DELIVERING（准备飞行配送）
                    from core.entities.primitives import TaskStatus
                    if order.status == TaskStatus.ASSIGNED:
                        order.update_status(TaskStatus.PICKED_UP)
                        order.update_status(TaskStatus.DELIVERING)
                    
                    # 设置初始状态
                    if alloc.mode == "B_WAIT":
                        # 无人机先由卡车运输，直到 launch_time 才起飞。
                        drone.current_loc = truck.get_location(current_time)
                        drone.status = DroneStatus.IDLE
                        drone.transport_truck_id = truck.truck_id
                        drone.scheduled_launch_time = alloc.launch_time
                        drone.launch_station_id = alloc.launch_station_id
                        drone.waiting_recovery_station_id = ""
                        if alloc.drone_id not in truck.docked_drones:
                            truck.docked_drones.append(alloc.drone_id)
                        logger.info(
                            "[DispatchDecisionEngine] 为无人机 %s 设置路由（模式 B_WAIT）："
                            "随卡车 %s 运输至充电站 %s（t=%.1fs）后起飞，"
                            "飞行 %.1fs → 回收点 %s，订单: %s",
                            alloc.drone_id, alloc.vehicle_id, alloc.launch_station_id,
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
                    # 模式 C：仓-空直递
                    # 无人机从仓库出发 → 配送点 → 仓库回收
                    depot = self.entity_mgr.depots.get(alloc.vehicle_id)
                    if depot is None:
                        logger.warning("[DispatchDecisionEngine] 仓库 %s 不存在", alloc.vehicle_id)
                        continue

                    depot_loc = depot.location
                    delivery_loc = order.delivery_loc

                    waypoints = [
                        RouteWaypoint(depot_loc, WaypointAction.PICKUP, alloc.order_id),
                        RouteWaypoint(delivery_loc, WaypointAction.DELIVER, alloc.order_id),
                        RouteWaypoint(depot_loc, WaypointAction.DOCK_DEPOT, alloc.vehicle_id),
                    ]
                    drone.set_route(waypoints)
                    # 为无人机分配订单
                    try:
                        drone.assign_order(alloc.order_id, order.payload_weight)
                    except ValueError as e:
                        logger.warning("[DispatchDecisionEngine] 为无人机 %s 分配订单失败: %s", alloc.drone_id, e)
                    
                    # 订单状态转为 PICKED_UP → DELIVERING（准备飞行配送）
                    from core.entities.primitives import TaskStatus
                    if order.status == TaskStatus.ASSIGNED:
                        order.update_status(TaskStatus.PICKED_UP)
                        order.update_status(TaskStatus.DELIVERING)
                    
                    drone.transport_truck_id = None
                    drone.scheduled_launch_time = 0.0
                    drone.launch_station_id = ""
                    drone.waiting_recovery_station_id = ""
                    drone.status = DroneStatus.FLYING_TO_PICKUP  # 设置初始飞行状态
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

                elif alloc.mode == "B_WAIT":
                    # 模式 B_WAIT：无人机在充电站等待后从该站点起飞
                    launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
                    if launch_station is None:
                        continue
                    
                    launch_loc = launch_station.location
                    delivery_loc = order.delivery_loc
                    recovery_id = alloc.recovery_station_id
                    recovery_station = self.entity_mgr.stations.get(recovery_id)
                    if recovery_station is None:
                        continue
                    recovery_loc = recovery_station.location

                    # 构建飞行路径：充电站 → 配送点 → 回收充电站
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
                        mode="B_WAIT",
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
