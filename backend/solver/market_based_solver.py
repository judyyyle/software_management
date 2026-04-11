#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 市场拍卖调度算法（支持动态增量调度与时空契约）

设计原则：
  1. 复用 GreedyBaseline 中已经稳定的能耗模型、卡车路径构建与 DispatchPlan 契约
  2. 将逐单"直接选模式"替换为"单任务顺序拍卖"
  3. 由协调器统一收集无人机 / 卡车投标，按综合成本最低授标
  4. 中标后即刻生成 RendezvousContract，锁定卡车锚点与无人机资源
  5. 支持 dispatch_incremental 实现事件驱动的动态新单接入

当前实现覆盖三类投标：
  - A: 卡车直递（卡车作为兜底竞标者）
  - B_WAIT: 基于卡车锚点时刻表的站点起飞 / 站点回收
  - C: 仓库直发 / 仓库回收

动态协议：
  - 每次 B_WAIT 中标，生成 RendezvousContract（不可移动的硬约束）
  - 卡车路径包含"自由段"（可插单）和"锁定段"（必须按时到达锚点）
  - 新单拍卖时，卡车必须校验插单不会导致任何已锁定契约超时
  - 评分函数引入非对称等待惩罚（机等车低权重，车等机高权重）
  - 接近生死线的锚点被选中时附加风险惩罚分

工程说明（与实现同步维护）：solver/market_based.md
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from solver.greedy_baseline import (
    AllocationResult,
    DispatchPlan,
    GreedyBaseline,
    _osm_svc,
    load_osm_from_cache,
)

if TYPE_CHECKING:
    from core.entities.drone import Drone
    from core.entities.order import Order
    from core.entities.primitives import Position3D
    from core.entities.truck import Truck

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 时空契约
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RendezvousContract:
    """中标后生成的时空约束。

    当一个 B_WAIT 订单中标，系统生成 RendezvousContract 锁定：
      - 卡车必须在 latest_departure 之前到达指定回收锚点
      - 关联无人机在 t_sync 之前不可被重新分配

    status 生命周期：
      active   → fulfilled （回收完成）
      active   → expired   （超过 latest_departure 仍未兑现）
      active   → released  （弹性归巢：无人机自主返回仓库，释放卡车锚点锁定）
    """
    contract_id: str
    truck_id: str
    drone_id: str
    order_id: str
    anchor_id: str
    launch_anchor_id: str = ""
    arrival_time: float = 0.0
    latest_departure: float = 0.0
    uav_arrival_time: float = 0.0
    t_sync: float = 0.0
    status: str = "active"


# ══════════════════════════════════════════════════════════════════════════════
# 市场拍卖调度器
# ══════════════════════════════════════════════════════════════════════════════

class MarketBasedSolver(GreedyBaseline):
    """基于锚点时刻表的单任务顺序拍卖调度器，支持动态增量拍卖与时空契约。"""

    T_MAX_WAIT = 60.0

    # 非对称等待惩罚权重（已校正：缩小机等车/车等机的极端不对称性）
    OMEGA_UAV_IDLE = 0.5         # 机等车：含机会成本（无法投标新单）
    OMEGA_TRUCK_IDLE = 1.0       # 车等机：阻塞卡车后续配送
    # 机等车的额外机会成本（每秒等待损失的竞标潜力）
    OPPORTUNITY_COST_PER_SEC = 0.1
    # 时刻表缓冲比例（应对路网拥堵等不确定性）
    TIMETABLE_SLACK_RATIO = 0.10
    # 接近生死线的风险惩罚权重（已校正：降低以减少保守退化为卡车直递）
    DEADLINE_RISK_WEIGHT = 0.8

    def __init__(self, entity_mgr) -> None:
        super().__init__(entity_mgr)
        self._auction_bid_count = 0
        self._auction_award_count = 0
        self._anchor_preview_routes: dict[str, object] = {}
        self._active_contracts: list[RendezvousContract] = []
        self._contract_seq = 0

    # ══════════════════════════════════════════════════════════════════════════
    # 调度入口
    # ══════════════════════════════════════════════════════════════════════════

    def dispatch(
        self,
        pending_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """执行一轮市场拍卖调度（全量批次），并在 summary 中附加拍卖统计信息。"""
        self._auction_bid_count = 0
        self._auction_award_count = 0
        self._anchor_preview_routes = {}
        self._expire_contracts(current_time)
        self._try_flexible_recovery(current_time)
        self._prepare_anchor_preview_routes(pending_orders, current_time, bbox, scene_id)

        plan = super().dispatch(pending_orders, current_time, bbox, scene_id=scene_id)
        self._recalculate_actual_plan_costs(plan, pending_orders, current_time)
        plan.summary["solver"] = "market"
        plan.summary["auction_stats"] = {
            "bids": self._auction_bid_count,
            "awards": self._auction_award_count,
            "active_contracts": len([c for c in self._active_contracts if c.status == "active"]),
        }
        logger.info(
            "[MarketBasedSolver] 分配完成：orders=%d feasible=%d bids=%d awards=%d contracts=%d",
            plan.summary.get("total_orders", 0),
            plan.summary.get("feasible", 0),
            self._auction_bid_count,
            self._auction_award_count,
            plan.summary["auction_stats"]["active_contracts"],
        )
        return plan

    def dispatch_incremental(
        self,
        new_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """事件驱动的增量拍卖：仅对动态新单执行调度，尊重已有契约。

        流程：
          1. 冻结状态：过期旧契约，获取当前卡车位置与活跃契约
          2. 构建保证时刻表：基于已锁定契约生成基础时刻表
          3. 增量拍卖：仅对 new_orders 收集投标并授标
          4. 双向锁定：中标后生成新契约，更新无人机/卡车锁定状态
          5. 返回增量计划（可与前序计划合并）
        """
        self._auction_bid_count = 0
        self._auction_award_count = 0
        self._expire_contracts(current_time)
        self._try_flexible_recovery(current_time)

        if not self._anchor_preview_routes:
            self._prepare_anchor_preview_routes(new_orders, current_time, bbox, scene_id)

        plan = super().dispatch(new_orders, current_time, bbox, scene_id=scene_id)
        self._recalculate_actual_plan_costs(plan, new_orders, current_time)
        plan.summary["solver"] = "market"
        plan.summary["dispatch_type"] = "incremental"
        plan.summary["auction_stats"] = {
            "bids": self._auction_bid_count,
            "awards": self._auction_award_count,
            "active_contracts": len([c for c in self._active_contracts if c.status == "active"]),
        }
        logger.info(
            "[MarketBasedSolver] 增量调度完成：new_orders=%d feasible=%d bids=%d awards=%d contracts=%d",
            plan.summary.get("total_orders", 0),
            plan.summary.get("feasible", 0),
            self._auction_bid_count,
            self._auction_award_count,
            plan.summary["auction_stats"]["active_contracts"],
        )
        return plan

    # ══════════════════════════════════════════════════════════════════════════
    # 成本重算（总体能耗公式不变）
    # ══════════════════════════════════════════════════════════════════════════

    def _recalculate_actual_plan_costs(
        self,
        plan: DispatchPlan,
        orders_by_id: dict[str, "Order"],
        current_time: float,
    ) -> None:
        """
        按最终真实执行路径重算总成本。

        说明：
          - 市场拍卖的授标阶段允许 B/B_WAIT 使用"边际卡车成本"参与竞价
          - 但最终统计必须计入卡车真实主干路线的全部距离与能耗
        """
        truck_distance_total = sum(route.total_distance for route in plan.truck_routes.values())
        truck_energy_total = sum(
            self._truck_energy_wh(route.total_distance)
            for route in plan.truck_routes.values()
        )

        uav_distance_total = 0.0
        uav_energy_total = 0.0
        penalty_total = sum(
            alloc.cost_penalty for alloc in plan.allocations
            if alloc.feasible and math.isfinite(alloc.cost_penalty)
        )

        for alloc in plan.allocations:
            if not alloc.feasible or alloc.mode == "A" or not alloc.drone_id:
                continue

            order = orders_by_id.get(alloc.order_id)
            drone = self.entity_mgr.drones.get(alloc.drone_id)
            if order is None or drone is None:
                continue

            if alloc.mode in ("B_WAIT", "B_DYNAMIC"):
                launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
                recovery = (
                    self.entity_mgr.stations.get(alloc.recovery_station_id)
                    or self.entity_mgr.depots.get(alloc.recovery_station_id)
                )
                if launch_station is None or recovery is None:
                    continue
                launch_loc = launch_station.location
                recovery_loc = recovery.location
            elif alloc.mode == "B":
                truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
                recovery = (
                    self.entity_mgr.stations.get(alloc.recovery_station_id)
                    or self.entity_mgr.depots.get(alloc.recovery_station_id)
                )
                if truck is None or recovery is None:
                    continue
                launch_loc = truck.get_location(current_time)
                recovery_loc = recovery.location
            elif alloc.mode == "C":
                depot = self.entity_mgr.depots.get(alloc.vehicle_id)
                if depot is None:
                    continue
                launch_loc = depot.location
                recovery_loc = depot.location
            else:
                continue

            dist_out = self._dist(launch_loc, order.delivery_loc)
            dist_back = self._dist(order.delivery_loc, recovery_loc)
            uav_distance_total += dist_out + dist_back
            uav_energy_total += self._uav_energy_wh(
                drone, launch_loc, order.delivery_loc, order.payload_weight
            )
            uav_energy_total += self._uav_energy_wh(
                drone, order.delivery_loc, recovery_loc, 0.0
            )

        cost_dist_total = self.C_DIST_ET * truck_distance_total + self.C_DIST_UAV * uav_distance_total
        cost_energy_total = self.C_ENERGY_ET * truck_energy_total + self.C_ENERGY_UAV * uav_energy_total

        plan.summary["cost_breakdown"] = {
            "dist": cost_dist_total,
            "energy": cost_energy_total,
            "penalty": penalty_total,
        }
        plan.summary["actual_route_costs"] = {
            "truck_distance_total": truck_distance_total,
            "truck_energy_total": truck_energy_total,
            "uav_distance_total": uav_distance_total,
            "uav_energy_total": uav_energy_total,
        }
        plan.cost_total = cost_dist_total + cost_energy_total + penalty_total

        logger.info(
            "[MarketBasedSolver] 按真实执行路径重算总成本: "
            "truck_dist=%.2f truck_energy=%.2f uav_dist=%.2f uav_energy=%.2f total=%.2f",
            truck_distance_total,
            truck_energy_total,
            uav_distance_total,
            uav_energy_total,
            plan.cost_total,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 契约管理
    # ══════════════════════════════════════════════════════════════════════════

    def _expire_contracts(self, current_time: float) -> None:
        """将已过生死线的契约标记为 expired。"""
        for contract in self._active_contracts:
            if contract.status == "active" and current_time > contract.latest_departure:
                contract.status = "expired"
                logger.info(
                    "[MarketBasedSolver][Contract] 契约过期: %s truck=%s anchor=%s deadline=%.1f now=%.1f",
                    contract.contract_id, contract.truck_id, contract.anchor_id,
                    contract.latest_departure, current_time,
                )

    def _try_flexible_recovery(self, current_time: float) -> None:
        """弹性归巢：若无人机飞回仓库比等待卡车回收更优，则释放契约。

        触发条件（全部满足）：
          1. 契约仍处于 active 状态
          2. 无人机在锚点等待卡车的剩余时间 T_wait > 飞回仓库时间 T_to_depot
          3. 无人机电量足以安全飞回仓库
        释放后无人机变为可用，可立即参与新订单竞标。
        """
        depots = list(self.entity_mgr.depots.values())
        if not depots:
            return
        depot = depots[0]

        for contract in self._active_contracts:
            if contract.status != "active":
                continue
            if current_time >= contract.t_sync:
                continue

            drone = self.entity_mgr.drones.get(contract.drone_id)
            if drone is None or drone.cruise_speed <= 0:
                continue

            anchor = (
                self.entity_mgr.stations.get(contract.anchor_id)
                or self.entity_mgr.depots.get(contract.anchor_id)
            )
            if anchor is None:
                continue

            t_wait = contract.t_sync - current_time
            dist_to_depot = self._dist(anchor.location, depot.location)
            t_to_depot = dist_to_depot / drone.cruise_speed

            if t_wait <= t_to_depot:
                continue

            energy_to_depot = self._flight_energy(drone, anchor.location, depot.location, 0.0)
            if energy_to_depot * self.ENERGY_SAFETY_FACTOR > drone.battery_current:
                continue

            contract.status = "released"
            logger.info(
                "[MarketBasedSolver][Contract] 弹性归巢释放契约 %s: drone=%s "
                "T_wait=%.1fs > T_to_depot=%.1fs, energy_ok=True",
                contract.contract_id, contract.drone_id,
                t_wait, t_to_depot,
            )

    def _create_contract(
        self,
        alloc: AllocationResult,
        order: "Order",
        current_time: float,
    ) -> RendezvousContract:
        """基于中标结果创建并存储 RendezvousContract。"""
        self._contract_seq += 1

        truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
        drone = self.entity_mgr.drones.get(alloc.drone_id)
        launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
        recovery = (
            self.entity_mgr.stations.get(alloc.recovery_station_id)
            or self.entity_mgr.depots.get(alloc.recovery_station_id)
        )

        # 卡车到达回收锚点的预计时间
        truck_arrival = current_time
        latest_departure = current_time + self.T_MAX_WAIT
        if truck is not None:
            anchors = self._build_anchor_timetable(truck, current_time)
            recovery_anchor = next(
                (a for a in anchors if a["anchor_id"] == alloc.recovery_station_id), None
            )
            if recovery_anchor is not None:
                truck_arrival = recovery_anchor["arrival_time"]
                latest_departure = recovery_anchor["latest_rendezvous_time"]

        # 无人机到达回收锚点的预计时间
        launch_time_est = (
            alloc.launch_time
            if alloc.launch_time > current_time
            else current_time + self.TRUCK_DRONE_LAUNCH_TIME
        )
        uav_arrival_time = launch_time_est
        if drone is not None and drone.cruise_speed > 0 and launch_station and recovery:
            dist_out = self._dist(launch_station.location, order.delivery_loc)
            dist_back = self._dist(order.delivery_loc, recovery.location)
            uav_arrival_time = (
                launch_time_est
                + dist_out / drone.cruise_speed
                + self.delivery_service_time
                + dist_back / drone.cruise_speed
            )

        t_sync = max(uav_arrival_time, truck_arrival) + self.TRUCK_DRONE_RECOVER_TIME

        contract = RendezvousContract(
            contract_id=f"RC-{self._contract_seq:04d}",
            truck_id=alloc.vehicle_id,
            drone_id=alloc.drone_id,
            order_id=alloc.order_id,
            anchor_id=alloc.recovery_station_id,
            launch_anchor_id=alloc.launch_station_id,
            arrival_time=truck_arrival,
            latest_departure=latest_departure,
            uav_arrival_time=uav_arrival_time,
            t_sync=t_sync,
        )
        self._active_contracts.append(contract)
        logger.info(
            "[MarketBasedSolver][Contract] 新建契约 %s: truck=%s drone=%s order=%s "
            "anchor=%s truck_arrival=%.1f deadline=%.1f uav_arrival=%.1f t_sync=%.1f",
            contract.contract_id, contract.truck_id, contract.drone_id,
            contract.order_id, contract.anchor_id,
            contract.arrival_time, contract.latest_departure,
            contract.uav_arrival_time, contract.t_sync,
        )
        return contract

    def _get_truck_contracts(self, truck_id: str) -> list[RendezvousContract]:
        """获取某辆卡车的所有活跃契约。"""
        return [c for c in self._active_contracts if c.truck_id == truck_id and c.status == "active"]

    def _is_drone_locked(self, drone_id: str, current_time: float) -> bool:
        """检查无人机是否被活跃契约锁定（尚未完成回收）。

        released / fulfilled / expired 状态的契约不再锁定无人机。
        """
        for contract in self._active_contracts:
            if contract.status == "active" and contract.drone_id == drone_id:
                if current_time < contract.t_sync:
                    return True
        return False

    def _validate_truck_insertion(
        self,
        truck: "Truck",
        new_order: "Order",
        current_time: float,
    ) -> bool:
        """校验卡车接受新订单后，是否仍能满足所有已锁定契约的时间窗。

        校验逻辑（双重检验）：
          1. 最短路径检验：卡车直接从当前位置到新订单再到各锚点的 ETA
          2. 已有路线检验：考虑卡车已有的中间停靠点，计算最晚锚点 ETA
        满足任一条 ETA > latest_departure 即判定不可行。
        """
        contracts = self._get_truck_contracts(truck.truck_id)
        if not contracts:
            return True

        truck_loc = truck.get_location(current_time)
        delivery_dist = self._dist(truck_loc, new_order.delivery_loc)
        if truck.speed <= 0:
            return False
        detour_time = delivery_dist / truck.speed + self.SERVICE_TIME_CUSTOMER

        for contract in contracts:
            anchor = (
                self.entity_mgr.stations.get(contract.anchor_id)
                or self.entity_mgr.depots.get(contract.anchor_id)
            )
            if anchor is None:
                continue

            # 检验 1：卡车绕路配送新单后到达锚点的 ETA
            dist_to_anchor = self._dist(new_order.delivery_loc, anchor.location)
            eta_at_anchor = current_time + detour_time + dist_to_anchor / truck.speed

            if eta_at_anchor > contract.latest_departure:
                logger.debug(
                    "[MarketBasedSolver] 卡车 %s 插单 %s 导致契约 %s 超时 "
                    "(eta=%.1f > deadline=%.1f)，该投标不可行",
                    truck.truck_id, new_order.order_id, contract.contract_id,
                    eta_at_anchor, contract.latest_departure,
                )
                return False

            # 检验 2：考虑已有时刻表中的延迟传播
            existing_stops = getattr(truck, "_planned_route_stops", None) or []
            cursor = int(getattr(truck, "_planned_route_cursor", 0))
            for stop in existing_stops[cursor:]:
                if stop.get("node_id") == contract.anchor_id:
                    original_arrival = float(stop.get("arrival_time", 0))
                    shifted_arrival = original_arrival + detour_time
                    if shifted_arrival > contract.latest_departure:
                        logger.debug(
                            "[MarketBasedSolver] 卡车 %s 插单 %s 导致已有停靠 %s 延迟 "
                            "(原到达 %.1f + 绕路 %.1f = %.1f > deadline %.1f)",
                            truck.truck_id, new_order.order_id, contract.anchor_id,
                            original_arrival, detour_time, shifted_arrival,
                            contract.latest_departure,
                        )
                        return False
                    break

        return True

    def fulfill_contract(self, contract_id: str) -> None:
        """外部（仿真引擎/决策引擎）通知契约已兑现。"""
        for contract in self._active_contracts:
            if contract.contract_id == contract_id and contract.status == "active":
                contract.status = "fulfilled"
                logger.info("[MarketBasedSolver][Contract] 契约已兑现: %s", contract_id)
                return

    # ══════════════════════════════════════════════════════════════════════════
    # 锚点预览路线
    # ══════════════════════════════════════════════════════════════════════════

    def _prepare_anchor_preview_routes(
        self,
        pending_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None,
    ) -> None:
        """
        为市场拍卖生成卡车主干路径预览，并据此广播真实锚点时刻表。

        优先级：
          1. 若卡车已有执行中的 planned_route_stops，则直接复用
          2. 否则基于本轮待调度订单，为每辆卡车构建一条预览主干路线
        """
        trucks = list(self.entity_mgr.trucks.values())
        if not trucks:
            return

        # 若已有执行中的真实停靠计划，则无需再做预览。
        has_runtime_plan = False
        for truck in trucks:
            planned_stops = getattr(truck, "_planned_route_stops", None)
            if planned_stops:
                has_runtime_plan = True
                break
        if has_runtime_plan:
            logger.info("[MarketBasedSolver] 锚点时刻表复用卡车当前运行计划")
            return

        road_graph, nodes = self._load_road_graph_for_market(bbox, scene_id)
        if road_graph is None or nodes is None:
            logger.warning("[MarketBasedSolver] 无法构建卡车主干预览路线，将回退到运行时计划/启发式锚点")
            return

        orders_by_truck: dict[str, list["Order"]] = {truck.truck_id: [] for truck in trucks}
        for order in pending_orders.values():
            nearest_truck = min(
                trucks,
                key=lambda truck: self._dist(truck.get_location(current_time), order.delivery_loc),
            )
            orders_by_truck[nearest_truck.truck_id].append(order)

        for truck in trucks:
            candidate_orders = orders_by_truck.get(truck.truck_id, [])
            if not candidate_orders:
                continue
            # 将契约锁定的站点作为必经回收点注入预览路线
            contract_stations = [c.anchor_id for c in self._get_truck_contracts(truck.truck_id)]
            try:
                preview_route = self._build_truck_route(
                    truck=truck,
                    orders=candidate_orders,
                    recovery_station_ids=contract_stations,
                    current_time=current_time,
                    road_graph=road_graph,
                    nodes=nodes,
                    recovery_station_wait_times=None,
                )
            except Exception as exc:
                logger.warning(
                    "[MarketBasedSolver] 卡车 %s 主干预览路线构建失败，回退到启发式锚点: %s",
                    truck.truck_id,
                    exc,
                )
                continue

            self._anchor_preview_routes[truck.truck_id] = preview_route
            logger.info(
                "[MarketBasedSolver] 卡车 %s 主干预览路线已生成：%d 节点，经停锚点 %s",
                truck.truck_id,
                len(preview_route.nodes),
                preview_route.charging_stop_ids,
            )

    def _load_road_graph_for_market(
        self,
        bbox: dict,
        scene_id: str | None,
    ):
        """复用贪心基线的 OSM 加载逻辑，为锚点时刻表生成预览路线。"""
        road_graph = None
        nodes = None

        if scene_id and load_osm_from_cache:
            try:
                osm_xml, _ = load_osm_from_cache(scene_id)
                if osm_xml:
                    road_graph, nodes = _osm_svc.build_road_graph(osm_xml)
            except Exception as exc:
                logger.warning("[MarketBasedSolver] 从场景缓存加载 OSM 失败: %s", exc)
                road_graph = None
                nodes = None

        if road_graph is None:
            try:
                osm_xml = _osm_svc.download_osm(
                    bbox["minx"], bbox["miny"], bbox["maxx"], bbox["maxy"]
                )
                road_graph, nodes = _osm_svc.build_road_graph(osm_xml)
            except Exception as exc:
                logger.warning("[MarketBasedSolver] 下载/构建 OSM 失败: %s", exc)
                return None, None

        return road_graph, nodes

    # ══════════════════════════════════════════════════════════════════════════
    # 单订单拍卖
    # ══════════════════════════════════════════════════════════════════════════

    def _allocate_order(
        self,
        order: "Order",
        current_time: float,
        allocated_drones: set[str],
    ) -> AllocationResult:
        """
        对单个订单执行单任务拍卖。

        流程：
          1. 先做硬过滤：超重订单直接交由卡车竞标
          2. 收集卡车投标（模式 A）—— 需通过契约合规校验
          3. 收集无人机投标（模式 B_WAIT / B / C）—— 排除契约锁定的无人机
          4. 选择综合评分最低的投标作为中标结果
          5. 若中标模式为 B_WAIT，立即生成 RendezvousContract
        """
        bids: list[AllocationResult] = []
        drone_diag: dict = {}

        if self._must_assign_to_truck(order):
            forced_truck_bid = self._best_truck_bid(order, current_time)
            if forced_truck_bid is not None:
                forced_truck_bid.reason = "订单超出无人机载重能力，强制由卡车执行"
                self._auction_bid_count += 1
                self._auction_award_count += 1
                self._log_bid_diagnostics(
                    order=order,
                    bids=[forced_truck_bid],
                    selected=forced_truck_bid,
                    drone_diag={
                        "forced_to_truck": True,
                        "max_drone_payload": max(
                            (d.payload_capacity for d in self.entity_mgr.drones.values()),
                            default=0.0,
                        ),
                    },
                )
                return forced_truck_bid
            return AllocationResult(
                order_id=order.order_id,
                vehicle_id="",
                mode="REJECT",
                distance=float("inf"),
                feasible=False,
                reason="订单超重且无可用卡车",
            )

        truck_bid = self._best_truck_bid(order, current_time)
        if truck_bid is not None:
            bids.append(truck_bid)

        bids.extend(self._collect_drone_bids(order, current_time, allocated_drones, drone_diag))
        self._auction_bid_count += len(bids)

        if not bids:
            self._log_bid_diagnostics(
                order=order,
                bids=[],
                selected=None,
                drone_diag=drone_diag,
            )
            return AllocationResult(
                order_id=order.order_id,
                vehicle_id="",
                mode="REJECT",
                distance=float("inf"),
                feasible=False,
                reason="拍卖失败：无可用卡车或无人机投标",
            )

        best = min(bids, key=lambda bid: bid.score_total)
        self._auction_award_count += 1

        # 中标后立即生成时空契约
        if best.mode == "B_WAIT" and best.feasible:
            self._create_contract(best, order, current_time)

        self._log_bid_diagnostics(order=order, bids=bids, selected=best, drone_diag=drone_diag)
        logger.info(
            "[MarketBasedSolver] 订单 %s 收到 %d 个投标，授标模式=%s 载体=%s score=%.2f",
            order.order_id,
            len(bids),
            best.mode,
            best.drone_id or best.vehicle_id,
            best.score_total,
        )
        return best

    def _must_assign_to_truck(self, order: "Order") -> bool:
        """若订单重量超过全局无人机最大载重，则直接走卡车模式。"""
        if not self.entity_mgr.drones:
            return True
        max_payload = max(d.payload_capacity for d in self.entity_mgr.drones.values())
        return order.payload_weight > max_payload

    def _collect_drone_bids(
        self,
        order: "Order",
        current_time: float,
        allocated_drones: set[str],
        diagnostics: dict | None = None,
    ) -> list[AllocationResult]:
        """收集所有可用无人机的竞标结果。

        包含两类竞标者：
          1. IDLE 无人机 — 标准竞标（B_WAIT / C / B_DYNAMIC）
          2. 执行中无人机 — 串联竞标（RELAY），以当前任务终点为起点
        """
        idle_drones = self._get_available_drones()
        available_drones = []
        excluded_allocated = 0
        excluded_payload = 0
        excluded_locked = 0
        for drone in idle_drones:
            if drone.drone_id in allocated_drones:
                excluded_allocated += 1
                continue
            if self._is_drone_locked(drone.drone_id, current_time):
                excluded_locked += 1
                continue
            if drone.payload_capacity < order.payload_weight:
                excluded_payload += 1
                continue
            available_drones.append(drone)

        if diagnostics is not None:
            diagnostics["idle_drones"] = len(idle_drones)
            diagnostics["excluded_allocated"] = excluded_allocated
            diagnostics["excluded_payload"] = excluded_payload
            diagnostics["excluded_locked"] = excluded_locked
            diagnostics["eligible_drones"] = len(available_drones)
            diagnostics["drones"] = []
            diagnostics["relay_candidates"] = 0
            diagnostics["relay_feasible"] = 0

        bids: list[AllocationResult] = []
        for drone in available_drones:
            drone_diag = {
                "drone_id": drone.drone_id,
                "anchor_candidates": 0,
                "anchor_feasible": 0,
                "anchor_energy_rejected": 0,
                "anchor_sync_rejected": 0,
                "depot_feasible": 0,
                "depot_energy_rejected": 0,
                "moving_candidates": 0,
                "moving_feasible": 0,
                "moving_energy_rejected": 0,
                "moving_sync_rejected": 0,
                "b_dynamic_candidates": 0,
                "b_dynamic_feasible": 0,
                "b_dynamic_energy_rejected": 0,
            }
            bids.extend(self._collect_anchor_bids_for_drone(drone, order, current_time, drone_diag))
            depot_bid = self._build_depot_bid(drone, order, current_time, drone_diag)
            if depot_bid is not None:
                bids.append(depot_bid)
            bids.extend(self._collect_b_dynamic_bids(drone, order, current_time, drone_diag))
            if self.ALLOW_MOVING_TRUCK_LAUNCH:
                bids.extend(self._collect_moving_truck_bids(drone, order, current_time, drone_diag))
            if diagnostics is not None:
                diagnostics["drones"].append(drone_diag)

        # 串联任务竞标：正在飞行的无人机以任务终点为起点竞标
        relay_bids = self._collect_relay_bids(order, current_time, allocated_drones, diagnostics)
        bids.extend(relay_bids)

        return bids

    def _best_truck_bid(
        self,
        order: "Order",
        current_time: float,
    ) -> AllocationResult | None:
        """卡车作为竞标者，对订单提交模式 A 的最低价投标（需通过契约合规校验）。"""
        trucks = list(self.entity_mgr.trucks.values())
        if not trucks:
            return None

        best_bid: AllocationResult | None = None
        for truck in trucks:
            if not self._validate_truck_insertion(truck, order, current_time):
                continue
            bid = AllocationResult(
                order_id=order.order_id,
                vehicle_id=truck.truck_id,
                mode="A",
                distance=self._dist(truck.get_location(current_time), order.delivery_loc),
                feasible=True,
            )
            scored = self._score_standard_bid(bid, order, current_time)
            if scored is None:
                continue
            if best_bid is None or scored.score_total < best_bid.score_total:
                best_bid = scored
        return best_bid

    def _build_depot_bid(
        self,
        drone: "Drone",
        order: "Order",
        current_time: float,
        diagnostics: dict | None = None,
    ) -> AllocationResult | None:
        """构造模式 C 的无人机投标。"""
        depots = list(self.entity_mgr.depots.values())
        if not depots:
            return None

        depot = depots[0]
        energy_out = self._flight_energy(drone, depot.location, order.delivery_loc, order.payload_weight)
        energy_back = self._flight_energy(drone, order.delivery_loc, depot.location, 0.0)
        energy_needed = (energy_out + energy_back) * self.ENERGY_SAFETY_FACTOR
        if energy_needed > drone.battery_current:
            if diagnostics is not None:
                diagnostics["depot_energy_rejected"] += 1
            return None

        bid = AllocationResult(
            order_id=order.order_id,
            vehicle_id=depot.depot_id,
            mode="C",
            distance=self._dist(depot.location, order.delivery_loc),
            feasible=True,
            recovery_station_id=depot.depot_id,
            drone_id=drone.drone_id,
        )
        if diagnostics is not None:
            diagnostics["depot_feasible"] += 1
        return self._score_standard_bid(bid, order, current_time)

    def _collect_anchor_bids_for_drone(
        self,
        drone: "Drone",
        order: "Order",
        current_time: float,
        diagnostics: dict | None = None,
    ) -> list[AllocationResult]:
        """
        基于卡车广播的锚点时刻表，为单架无人机生成 B_WAIT 投标。

        约束：
          - 起飞锚点与回收锚点均必须属于同一辆卡车的预测锚点序列
          - 回收锚点顺序不得早于起飞锚点
          - 需满足前瞻能量校验
        """
        bids: list[AllocationResult] = []

        for truck in self.entity_mgr.trucks.values():
            if not self._validate_truck_insertion(truck, order, current_time):
                continue

            anchors = self._build_anchor_timetable(truck, current_time)
            if not anchors:
                continue

            for launch_idx, launch_anchor in enumerate(anchors):
                for recovery_anchor in anchors[launch_idx:]:
                    if diagnostics is not None:
                        diagnostics["anchor_candidates"] += 1
                    bid = self._build_anchor_bid(
                        drone=drone,
                        truck=truck,
                        order=order,
                        current_time=current_time,
                        launch_anchor=launch_anchor,
                        recovery_anchor=recovery_anchor,
                        diagnostics=diagnostics,
                    )
                    if bid is not None:
                        bids.append(bid)

        return bids

    def _collect_b_dynamic_bids(
        self,
        drone: "Drone",
        order: "Order",
        current_time: float,
        diagnostics: dict | None = None,
    ) -> list[AllocationResult]:
        """站点起飞 + 仓库回收的混合投标（B_DYNAMIC）。

        适用场景：卡车远离仓库，但订单和仓库距离较近时，无人机从卡车锚点
        起飞送达后直飞仓库，无需等待卡车回收，节省等待时间并释放无人机产能。
        """
        depots = list(self.entity_mgr.depots.values())
        if not depots:
            return []
        depot = depots[0]
        bids: list[AllocationResult] = []

        for truck in self.entity_mgr.trucks.values():
            anchors = self._build_anchor_timetable(truck, current_time)
            if not anchors:
                continue

            for launch_anchor in anchors:
                if diagnostics is not None:
                    diagnostics["b_dynamic_candidates"] += 1

                launch_loc = launch_anchor["location"]
                launch_time = launch_anchor["eta"] + self.TRUCK_DRONE_LAUNCH_TIME

                dist_out = self._dist(launch_loc, order.delivery_loc)
                dist_to_depot = self._dist(order.delivery_loc, depot.location)

                energy_out_j = self._flight_energy(
                    drone, launch_loc, order.delivery_loc, order.payload_weight
                )
                energy_to_depot_j = self._flight_energy(
                    drone, order.delivery_loc, depot.location, 0.0
                )
                total_energy_j = (energy_out_j + energy_to_depot_j) * self.ENERGY_SAFETY_FACTOR
                if total_energy_j > drone.battery_current:
                    if diagnostics is not None:
                        diagnostics["b_dynamic_energy_rejected"] += 1
                    continue

                bid = AllocationResult(
                    order_id=order.order_id,
                    vehicle_id=truck.truck_id,
                    mode="B_DYNAMIC",
                    distance=dist_out,
                    feasible=True,
                    recovery_station_id=depot.depot_id,
                    drone_id=drone.drone_id,
                    launch_station_id=launch_anchor["anchor_id"],
                    launch_time=launch_time,
                    wait_duration=(
                        self.TRUCK_DRONE_LAUNCH_TIME
                        + dist_out / drone.cruise_speed
                        + self.delivery_service_time
                        + dist_to_depot / drone.cruise_speed
                    ),
                )
                scored = self._score_standard_bid(bid, order, current_time)
                if scored is not None:
                    if diagnostics is not None:
                        diagnostics["b_dynamic_feasible"] += 1
                    bids.append(scored)

        return bids

    def _collect_relay_bids(
        self,
        order: "Order",
        current_time: float,
        allocated_drones: set[str],
        diagnostics: dict | None = None,
    ) -> list[AllocationResult]:
        """串联任务竞标：正在飞行的无人机以当前任务终点为起点竞标新订单。

        适用场景：无人机完成第一单配送后，若电量支持且有更近的动态订单，
        可在不返回基站的情况下直接飞向下一单。第二单完成后强制飞回仓库。

        竞标起点 = 当前任务的最终交付点（DELIVER 航路点坐标）。
        """
        depots = list(self.entity_mgr.depots.values())
        if not depots:
            return []
        depot = depots[0]

        bids: list[AllocationResult] = []
        for drone in self.entity_mgr.drones.values():
            if drone.drone_id in allocated_drones:
                continue
            if not drone.status.is_flying:
                continue
            if drone.payload_capacity < order.payload_weight:
                continue

            # 找到当前路径中的交付点作为串联起点
            relay_origin = self._get_drone_relay_origin(drone)
            if relay_origin is None:
                continue
            if diagnostics is not None:
                diagnostics["relay_candidates"] = diagnostics.get("relay_candidates", 0) + 1

            # 估算剩余电量（粗略：当前电量 - 完成当前任务所需的能量）
            remaining_energy = self._estimate_relay_remaining_energy(drone, relay_origin)
            if remaining_energy <= 0:
                continue

            # 串联路径：relay_origin → customer → depot
            dist_to_customer = self._dist(relay_origin, order.delivery_loc)
            dist_to_depot = self._dist(order.delivery_loc, depot.location)
            energy_to_customer = self._flight_energy(drone, relay_origin, order.delivery_loc, order.payload_weight)
            energy_to_depot = self._flight_energy(drone, order.delivery_loc, depot.location, 0.0)
            energy_needed = (energy_to_customer + energy_to_depot) * self.ENERGY_SAFETY_FACTOR

            if energy_needed > remaining_energy:
                continue

            # 估算可用时间
            time_to_available = self._estimate_relay_available_time(drone, relay_origin, current_time)
            time_to_customer = dist_to_customer / drone.cruise_speed
            eta_at_customer = current_time + time_to_available + time_to_customer

            if eta_at_customer > order.deadline:
                continue

            bid = AllocationResult(
                order_id=order.order_id,
                vehicle_id=depot.depot_id,
                mode="C",
                distance=dist_to_customer + dist_to_depot,
                feasible=True,
                recovery_station_id=depot.depot_id,
                drone_id=drone.drone_id,
                launch_time=current_time + time_to_available,
            )
            bid._relay_origin = relay_origin
            bid._is_relay = True

            scored = self._score_standard_bid(bid, order, current_time)
            if scored is not None:
                scored._relay_origin = relay_origin
                scored._is_relay = True
                bids.append(scored)
                if diagnostics is not None:
                    diagnostics["relay_feasible"] = diagnostics.get("relay_feasible", 0) + 1

        return bids

    def _get_drone_relay_origin(self, drone) -> "Position3D | None":
        """获取无人机串联竞标的起点（当前路径中最后一个 DELIVER 点）。"""
        from core.entities.primitives import WaypointAction
        last_deliver = None
        for wp in drone.route_plan:
            if wp.action == WaypointAction.DELIVER:
                last_deliver = wp.loc
        return last_deliver

    def _estimate_relay_remaining_energy(self, drone, relay_origin) -> float:
        """估算无人机到达串联起点时的剩余电量。"""
        current_energy = drone.battery_current
        if not drone.has_pending_route:
            return current_energy

        # 估算完成当前路径的能耗
        pos = drone.current_loc
        for i in range(drone.current_waypoint_index, len(drone.route_plan)):
            wp = drone.route_plan[i]
            energy = self._flight_energy(drone, pos, wp.loc, drone.current_payload)
            current_energy -= energy
            pos = wp.loc
            if current_energy <= 0:
                return 0.0
        return max(0.0, current_energy)

    def _estimate_relay_available_time(self, drone, relay_origin, current_time: float) -> float:
        """估算无人机到达串联起点的时间（完成当前路径所需时间）。"""
        if not drone.has_pending_route:
            return 0.0

        total_dist = 0.0
        pos = drone.current_loc
        for i in range(drone.current_waypoint_index, len(drone.route_plan)):
            wp = drone.route_plan[i]
            total_dist += self._dist(pos, wp.loc)
            pos = wp.loc
        return total_dist / drone.cruise_speed + self.delivery_service_time

    def _collect_moving_truck_bids(
        self,
        drone: "Drone",
        order: "Order",
        current_time: float,
        diagnostics: dict | None = None,
    ) -> list[AllocationResult]:
        """在允许的情况下，生成从卡车当前位置直接起飞的模式 B 投标。"""
        bids: list[AllocationResult] = []

        for truck in self.entity_mgr.trucks.values():
            truck_loc = truck.get_location(current_time)
            anchors = self._build_anchor_timetable(truck, current_time)
            if not anchors:
                continue

            truck_distance_to_launch = 0.0
            launch_time = current_time + self.TRUCK_DRONE_LAUNCH_TIME
            dist_out = self._dist(truck_loc, order.delivery_loc)
            energy_out_j = self._flight_energy(drone, truck_loc, order.delivery_loc, order.payload_weight)

            for recovery_anchor in anchors:
                if diagnostics is not None:
                    diagnostics["moving_candidates"] += 1
                recovery_loc = recovery_anchor["location"]
                dist_back = self._dist(order.delivery_loc, recovery_loc)
                energy_back_j = self._flight_energy(drone, order.delivery_loc, recovery_loc, 0.0)
                total_energy_j = (energy_out_j + energy_back_j) * self.ENERGY_SAFETY_FACTOR
                if total_energy_j > drone.battery_current:
                    if diagnostics is not None:
                        diagnostics["moving_energy_rejected"] += 1
                    continue

                uav_arrival_recovery = (
                    launch_time
                    + dist_out / drone.cruise_speed
                    + self.delivery_service_time
                    + dist_back / drone.cruise_speed
                )
                latest_rendezvous = recovery_anchor["latest_rendezvous_time"]
                if uav_arrival_recovery > latest_rendezvous:
                    if diagnostics is not None:
                        diagnostics["moving_sync_rejected"] += 1
                    continue
                bid = AllocationResult(
                    order_id=order.order_id,
                    vehicle_id=truck.truck_id,
                    mode="B",
                    distance=dist_out,
                    feasible=True,
                    recovery_station_id=recovery_anchor["anchor_id"],
                    drone_id=drone.drone_id,
                )
                scored = self._score_standard_bid(bid, order, current_time)
                if scored is not None:
                    if diagnostics is not None:
                        diagnostics["moving_feasible"] += 1
                    bids.append(scored)

        return bids

    # ══════════════════════════════════════════════════════════════════════════
    # 锚点时刻表
    # ══════════════════════════════════════════════════════════════════════════

    def _build_anchor_timetable(
        self,
        truck: "Truck",
        current_time: float,
    ) -> list[dict]:
        """基于卡车真实主干路径或运行时计划生成锚点时刻表，
        并合并活跃契约的硬约束锚点、对非锁定锚点施加缓冲时间。"""
        if truck.speed <= 0:
            return []

        anchors: list[dict] = []

        # ── 第一优先级：预览路线 ─────────────────────────────────────
        preview_route = self._anchor_preview_routes.get(truck.truck_id)
        if preview_route is not None:
            for node in preview_route.nodes:
                if node.node_type != "station":
                    continue
                latest_rendezvous_time = node.departure_time + self.T_MAX_WAIT
                anchors.append(
                    {
                        "index": len(anchors),
                        "anchor_id": node.node_id,
                        "location": node.position,
                        "eta": node.arrival_time,
                        "arrival_time": node.arrival_time,
                        "latest_rendezvous_time": latest_rendezvous_time,
                        "departure_time": latest_rendezvous_time + self.TRUCK_DRONE_RECOVER_TIME,
                        "distance_from_truck": max(
                            0.0,
                            (node.arrival_time - current_time) * truck.speed,
                        ),
                        "locked": False,
                    }
                )
            if anchors:
                return self._merge_contracts_into_timetable(anchors, truck, current_time)

        # ── 第二优先级：运行时停靠计划 ──────────────────────────────
        planned_stops = getattr(truck, "_planned_route_stops", None)
        if planned_stops:
            for stop in planned_stops:
                if stop.get("node_type") != "station":
                    continue
                position = stop.get("position")
                arrival_time = float(stop.get("arrival_time", current_time))
                latest_rendezvous_time = arrival_time + self.T_MAX_WAIT
                anchors.append(
                    {
                        "index": len(anchors),
                        "anchor_id": str(stop.get("node_id")),
                        "location": position,
                        "eta": arrival_time,
                        "arrival_time": arrival_time,
                        "latest_rendezvous_time": latest_rendezvous_time,
                        "departure_time": latest_rendezvous_time + self.TRUCK_DRONE_RECOVER_TIME,
                        "distance_from_truck": max(0.0, (arrival_time - current_time) * truck.speed),
                        "locked": False,
                    }
                )
            if anchors:
                return self._merge_contracts_into_timetable(anchors, truck, current_time)

        # ── 第三优先级：兼容回退（旧启发式） ────────────────────────
        truck_loc = truck.get_location(current_time)
        predicted_stations = self._predict_truck_charging_stations(truck, current_time)

        prev_loc = truck_loc
        cumulative_distance = 0.0
        for index, (anchor_id, anchor_loc) in enumerate(predicted_stations):
            leg_distance = self._dist(prev_loc, anchor_loc)
            cumulative_distance += leg_distance
            arrival_time = current_time + cumulative_distance / truck.speed
            latest_rendezvous_time = arrival_time + self.T_MAX_WAIT
            anchors.append(
                {
                    "index": index,
                    "anchor_id": anchor_id,
                    "location": anchor_loc,
                    "eta": arrival_time,
                    "arrival_time": arrival_time,
                    "latest_rendezvous_time": latest_rendezvous_time,
                    "departure_time": latest_rendezvous_time + self.TRUCK_DRONE_RECOVER_TIME,
                    "distance_from_truck": cumulative_distance,
                    "locked": False,
                }
            )
            prev_loc = anchor_loc

        return self._merge_contracts_into_timetable(anchors, truck, current_time)

    def _merge_contracts_into_timetable(
        self,
        anchors: list[dict],
        truck: "Truck",
        current_time: float,
    ) -> list[dict]:
        """将活跃契约的硬约束锚点合并入时刻表，并对非锁定锚点施加缓冲时间。"""
        contracts = self._get_truck_contracts(truck.truck_id)

        # 合并契约锁定的锚点
        existing_ids = {a["anchor_id"] for a in anchors}
        for contract in contracts:
            if contract.anchor_id in existing_ids:
                for anchor in anchors:
                    if anchor["anchor_id"] == contract.anchor_id:
                        anchor["locked"] = True
                        anchor["latest_rendezvous_time"] = min(
                            anchor["latest_rendezvous_time"],
                            contract.latest_departure,
                        )
                        break
            else:
                station = (
                    self.entity_mgr.stations.get(contract.anchor_id)
                    or self.entity_mgr.depots.get(contract.anchor_id)
                )
                if station is not None:
                    anchors.append(
                        {
                            "index": len(anchors),
                            "anchor_id": contract.anchor_id,
                            "location": station.location,
                            "eta": contract.arrival_time,
                            "arrival_time": contract.arrival_time,
                            "latest_rendezvous_time": contract.latest_departure,
                            "departure_time": contract.latest_departure + self.TRUCK_DRONE_RECOVER_TIME,
                            "distance_from_truck": max(
                                0.0,
                                (contract.arrival_time - current_time) * truck.speed,
                            ),
                            "locked": True,
                        }
                    )

        # 对非锁定锚点施加缓冲时间（Slack Time）
        for anchor in anchors:
            if not anchor.get("locked"):
                anchor["latest_rendezvous_time"] = (
                    anchor["arrival_time"]
                    + self.T_MAX_WAIT * (1 - self.TIMETABLE_SLACK_RATIO)
                )

        anchors.sort(key=lambda a: a["arrival_time"])
        for i, anchor in enumerate(anchors):
            anchor["index"] = i

        return anchors

    # ══════════════════════════════════════════════════════════════════════════
    # 单条投标构建
    # ══════════════════════════════════════════════════════════════════════════

    def _build_anchor_bid(
        self,
        drone: "Drone",
        truck: "Truck",
        order: "Order",
        current_time: float,
        launch_anchor: dict,
        recovery_anchor: dict,
        diagnostics: dict | None = None,
    ) -> AllocationResult | None:
        """对固定的"卡车-起飞锚点-回收锚点-无人机"组合生成一条投标。"""
        launch_loc: "Position3D" = launch_anchor["location"]
        recovery_loc: "Position3D" = recovery_anchor["location"]
        truck_distance_to_launch = launch_anchor["distance_from_truck"]
        launch_time = launch_anchor["eta"] + self.TRUCK_DRONE_LAUNCH_TIME

        dist_out = self._dist(launch_loc, order.delivery_loc)
        dist_back = self._dist(order.delivery_loc, recovery_loc)

        energy_out_j = self._flight_energy(drone, launch_loc, order.delivery_loc, order.payload_weight)
        energy_back_j = self._flight_energy(drone, order.delivery_loc, recovery_loc, 0.0)
        total_energy_j = (energy_out_j + energy_back_j) * self.ENERGY_SAFETY_FACTOR
        if total_energy_j > drone.battery_current:
            if diagnostics is not None:
                diagnostics["anchor_energy_rejected"] += 1
            return None

        uav_arrival_recovery = (
            launch_time
            + dist_out / drone.cruise_speed
            + self.delivery_service_time
            + dist_back / drone.cruise_speed
        )
        latest_rendezvous = recovery_anchor["latest_rendezvous_time"]
        if uav_arrival_recovery > latest_rendezvous:
            if diagnostics is not None:
                diagnostics["anchor_sync_rejected"] += 1
            return None
        bid = AllocationResult(
            order_id=order.order_id,
            vehicle_id=truck.truck_id,
            mode="B_WAIT",
            distance=dist_out,
            feasible=True,
            recovery_station_id=recovery_anchor["anchor_id"],
            drone_id=drone.drone_id,
            launch_station_id=launch_anchor["anchor_id"],
            launch_time=launch_time,
            wait_duration=(
                self.TRUCK_DRONE_LAUNCH_TIME
                + dist_out / drone.cruise_speed
                + self.delivery_service_time
                + dist_back / drone.cruise_speed
            ),
        )
        if diagnostics is not None:
            diagnostics["anchor_feasible"] += 1
        return self._score_standard_bid(bid, order, current_time)

    # ══════════════════════════════════════════════════════════════════════════
    # 评分
    # ══════════════════════════════════════════════════════════════════════════

    def _score_standard_bid(
        self,
        alloc: AllocationResult,
        order: "Order",
        current_time: float,
    ) -> AllocationResult | None:
        """
        统一复用贪心基线中的目标函数打分。

        对市场算法中的 B / B_WAIT，卡车主干路线属于既定基础设施成本，
        因此仅将"额外引入的边际卡车成本"计入协同任务评分，而不再把
        "卡车当前位置 -> 起飞锚点"的整段主干路径全量重复计费。
        """
        if alloc.mode in ("B", "B_WAIT"):
            score_total, cost_dist, cost_energy, cost_penalty = self._score_market_b_bid(
                alloc, order, current_time
            )
        elif alloc.mode == "B_DYNAMIC":
            score_total, cost_dist, cost_energy, cost_penalty = self._score_b_dynamic_bid(
                alloc, order, current_time
            )
        else:
            score_total, cost_dist, cost_energy, cost_penalty = self._score_allocation(
                alloc, order, current_time
            )
        if not math.isfinite(score_total):
            return None
        alloc.score_total = score_total
        alloc.cost_dist = cost_dist
        alloc.cost_energy = cost_energy
        alloc.cost_penalty = cost_penalty
        return alloc

    def _score_market_b_bid(
        self,
        alloc: AllocationResult,
        order: "Order",
        current_time: float,
    ) -> tuple[float, float, float, float]:
        """
        市场拍卖中的协同模式评分。

        基础成本（不变）：
          - UAV 飞行距离与飞行能耗仍按基线口径全量计入
          - 卡车部分只计"边际增量成本"，若锚点本就在卡车主干路线上，则增量视为 0

        动态扩展（新增）：
          - cost_wait: 非对称等待惩罚（机等车低权重 / 车等机高权重）
          - cost_risk: 接近生死线的锚点被选中时附加风险惩罚
        """
        drone = self.entity_mgr.drones.get(alloc.drone_id)
        if drone is None or drone.cruise_speed <= 0:
            return float("inf"), 0.0, 0.0, 0.0

        truck_distance = self._estimate_incremental_truck_distance(alloc, current_time)
        if not math.isfinite(truck_distance):
            return float("inf"), 0.0, 0.0, 0.0
        truck_energy = self._truck_energy_wh(truck_distance)

        if alloc.mode == "B_WAIT":
            truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
            launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
            if truck is None or launch_station is None:
                return float("inf"), 0.0, 0.0, 0.0
            launch_loc = launch_station.location
            launch_time_est = (
                alloc.launch_time
                if alloc.launch_time > current_time
                else current_time + self.TRUCK_DRONE_LAUNCH_TIME
            )
        else:
            truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
            if truck is None:
                return float("inf"), 0.0, 0.0, 0.0
            launch_loc = truck.get_location(current_time)
            launch_time_est = current_time + self.TRUCK_DRONE_LAUNCH_TIME

        recovery = (
            self.entity_mgr.stations.get(alloc.recovery_station_id)
            or self.entity_mgr.depots.get(alloc.recovery_station_id)
        )
        if recovery is None:
            return float("inf"), 0.0, 0.0, 0.0

        dist_out = self._dist(launch_loc, order.delivery_loc)
        dist_back = self._dist(order.delivery_loc, recovery.location)
        uav_distance = dist_out + dist_back
        uav_energy = self._uav_energy_wh(drone, launch_loc, order.delivery_loc, order.payload_weight)
        uav_energy += self._uav_energy_wh(drone, order.delivery_loc, recovery.location, 0.0)
        delivery_time_est = launch_time_est + dist_out / drone.cruise_speed + self.delivery_service_time

        cost_dist = self.C_DIST_ET * truck_distance + self.C_DIST_UAV * uav_distance
        cost_energy = self.C_ENERGY_ET * truck_energy + self.C_ENERGY_UAV * uav_energy
        lateness = max(0.0, delivery_time_est - order.deadline)
        cost_penalty = self.LAMBDA_TIME * order.penalty_rate * lateness

        # ── 非对称等待惩罚 ────────────────────────────────────────
        cost_wait = 0.0
        uav_arrival_at_recovery = delivery_time_est + dist_back / drone.cruise_speed
        recovery_anchor = None
        if truck is not None:
            for anchor in self._build_anchor_timetable(truck, current_time):
                if anchor["anchor_id"] == alloc.recovery_station_id:
                    recovery_anchor = anchor
                    break

        if recovery_anchor is not None:
            truck_arrival_at_recovery = recovery_anchor["arrival_time"]
            wait_diff = truck_arrival_at_recovery - uav_arrival_at_recovery
            if wait_diff > 0:
                # 机等车：基础等待 + 机会成本（无法竞标新订单的损失）
                cost_wait = (self.OMEGA_UAV_IDLE + self.OPPORTUNITY_COST_PER_SEC) * wait_diff
            else:
                cost_wait = self.OMEGA_TRUCK_IDLE * abs(wait_diff)

        # ── 接近生死线的风险惩罚 ──────────────────────────────────
        cost_risk = 0.0
        if recovery_anchor is not None:
            latest_rendezvous = recovery_anchor["latest_rendezvous_time"]
            margin = latest_rendezvous - uav_arrival_at_recovery
            if margin > 0 and self.T_MAX_WAIT > 0:
                risk_ratio = 1.0 - min(margin / self.T_MAX_WAIT, 1.0)
                cost_risk = self.DEADLINE_RISK_WEIGHT * risk_ratio * (cost_dist + cost_energy)

        score_total = cost_dist + cost_energy + cost_penalty + cost_wait + cost_risk
        return score_total, cost_dist, cost_energy, cost_penalty

    def _score_b_dynamic_bid(
        self,
        alloc: AllocationResult,
        order: "Order",
        current_time: float,
    ) -> tuple[float, float, float, float]:
        """B_DYNAMIC 评分：站点起飞 + 仓库回收，不含等待惩罚和风险溢价。

        无人机从锚点起飞送达后直飞仓库，无需与卡车同步：
          - 卡车只计 launch 段边际距离（锚点在路线上则为 0）
          - UAV 计全量距离/能耗（launch→customer→depot）
          - 无 cost_wait（不等卡车）、无 cost_risk（不依赖锚点生死线）
        """
        drone = self.entity_mgr.drones.get(alloc.drone_id)
        if drone is None or drone.cruise_speed <= 0:
            return float("inf"), 0.0, 0.0, 0.0

        depot = self.entity_mgr.depots.get(alloc.recovery_station_id)
        if depot is None:
            return float("inf"), 0.0, 0.0, 0.0

        launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
        if launch_station is None:
            return float("inf"), 0.0, 0.0, 0.0

        launch_loc = launch_station.location

        truck_distance = self._estimate_incremental_truck_distance(alloc, current_time)
        if not math.isfinite(truck_distance):
            return float("inf"), 0.0, 0.0, 0.0
        truck_energy = self._truck_energy_wh(truck_distance)

        dist_out = self._dist(launch_loc, order.delivery_loc)
        dist_to_depot = self._dist(order.delivery_loc, depot.location)
        uav_distance = dist_out + dist_to_depot
        uav_energy = self._uav_energy_wh(drone, launch_loc, order.delivery_loc, order.payload_weight)
        uav_energy += self._uav_energy_wh(drone, order.delivery_loc, depot.location, 0.0)

        launch_time_est = (
            alloc.launch_time
            if alloc.launch_time > current_time
            else current_time + self.TRUCK_DRONE_LAUNCH_TIME
        )
        delivery_time_est = launch_time_est + dist_out / drone.cruise_speed + self.delivery_service_time

        cost_dist = self.C_DIST_ET * truck_distance + self.C_DIST_UAV * uav_distance
        cost_energy = self.C_ENERGY_ET * truck_energy + self.C_ENERGY_UAV * uav_energy
        lateness = max(0.0, delivery_time_est - order.deadline)
        cost_penalty = self.LAMBDA_TIME * order.penalty_rate * lateness

        score_total = cost_dist + cost_energy + cost_penalty
        return score_total, cost_dist, cost_energy, cost_penalty

    def _estimate_incremental_truck_distance(
        self,
        alloc: AllocationResult,
        current_time: float,
    ) -> float:
        """
        估算协同任务给卡车带来的边际增量距离。

        若起飞/回收锚点已属于卡车真实主干路线，则卡车为该协同任务新增的
        行驶距离视为 0；否则退化回旧口径进行保守估算。
        """
        if alloc.mode == "B":
            return 0.0

        if alloc.mode not in ("B_WAIT", "B_DYNAMIC"):
            return 0.0

        truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
        launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
        if truck is None or launch_station is None:
            return float("inf")

        anchor_ids = {
            anchor["anchor_id"]
            for anchor in self._build_anchor_timetable(truck, current_time)
        }

        if alloc.mode == "B_DYNAMIC":
            if alloc.launch_station_id and alloc.launch_station_id in anchor_ids:
                return 0.0
            return self._dist(truck.get_location(current_time), launch_station.location)

        if (
            alloc.launch_station_id
            and alloc.recovery_station_id
            and alloc.launch_station_id in anchor_ids
            and alloc.recovery_station_id in anchor_ids
        ):
            return 0.0

        return self._dist(truck.get_location(current_time), launch_station.location)

    # ══════════════════════════════════════════════════════════════════════════
    # 诊断日志
    # ══════════════════════════════════════════════════════════════════════════

    def _log_bid_diagnostics(
        self,
        order: "Order",
        bids: list[AllocationResult],
        selected: AllocationResult | None,
        drone_diag: dict,
    ) -> None:
        """打印每单投标明细与无人机候选过滤统计。"""
        active_contracts = len([c for c in self._active_contracts if c.status == "active"])
        logger.info(
            "[MarketBasedSolver][Diag] 订单 %s payload=%.2fkg deadline=%.1f bids=%d contracts=%d",
            order.order_id,
            order.payload_weight,
            order.deadline,
            len(bids),
            active_contracts,
        )

        if drone_diag.get("forced_to_truck"):
            logger.info(
                "[MarketBasedSolver][Diag] 订单 %s 强制卡车：payload=%.2fkg > max_drone_payload=%.2fkg",
                order.order_id,
                order.payload_weight,
                drone_diag.get("max_drone_payload", 0.0),
            )
        else:
            logger.info(
                "[MarketBasedSolver][Diag] 订单 %s 无人机候选：idle=%d eligible=%d "
                "excluded_allocated=%d excluded_payload=%d excluded_locked=%d",
                order.order_id,
                drone_diag.get("idle_drones", 0),
                drone_diag.get("eligible_drones", 0),
                drone_diag.get("excluded_allocated", 0),
                drone_diag.get("excluded_payload", 0),
                drone_diag.get("excluded_locked", 0),
            )

            for item in drone_diag.get("drones", []):
                logger.info(
                    "[MarketBasedSolver][Diag] 订单 %s 无人机 %s: "
                    "anchor %d/%d (energy_rej=%d sync_rej=%d) | "
                    "depot %d (energy_rej=%d) | "
                    "b_dynamic %d/%d (energy_rej=%d) | "
                    "moving %d/%d (energy_rej=%d sync_rej=%d)",
                    order.order_id,
                    item["drone_id"],
                    item["anchor_feasible"],
                    item["anchor_candidates"],
                    item["anchor_energy_rejected"],
                    item["anchor_sync_rejected"],
                    item["depot_feasible"],
                    item["depot_energy_rejected"],
                    item.get("b_dynamic_feasible", 0),
                    item.get("b_dynamic_candidates", 0),
                    item.get("b_dynamic_energy_rejected", 0),
                    item["moving_feasible"],
                    item["moving_candidates"],
                    item["moving_energy_rejected"],
                    item["moving_sync_rejected"],
                )

        for bid in sorted(bids, key=lambda x: x.score_total):
            logger.info(
                "[MarketBasedSolver][Bid] 订单 %s mode=%s vehicle=%s drone=%s "
                "launch=%s recovery=%s score=%.2f dist=%.2f energy=%.2f penalty=%.2f",
                order.order_id,
                bid.mode,
                bid.vehicle_id or "-",
                bid.drone_id or "-",
                bid.launch_station_id or "-",
                bid.recovery_station_id or "-",
                bid.score_total,
                bid.cost_dist,
                bid.cost_energy,
                bid.cost_penalty,
            )

        if selected is not None:
            logger.info(
                "[MarketBasedSolver][Selected] 订单 %s -> mode=%s vehicle=%s drone=%s score=%.2f",
                order.order_id,
                selected.mode,
                selected.vehicle_id or "-",
                selected.drone_id or "-",
                selected.score_total,
            )
