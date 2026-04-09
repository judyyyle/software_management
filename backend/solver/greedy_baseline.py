#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 贪心调度算法（Baseline v2）

核心改进：
  1. 前瞻能量校验：分配无人机任务前，验证电量能支撑送达 + 安全降落到最近回收点
  2. 卡车路径规划：最近邻启发式（按时间窗紧迫度排序）+ 充电站顺路插入
  3. 无人机回收点选择：在充电站 + 仓库候选池中找到能量可行且最省电的降落点
  4. 模式优先级：B（卡-空协同）> C（仓-空直递）> A（卡车直递）

算法流程：
  Phase 1  按剩余时间窗紧迫度升序排列订单
  Phase 2  逐单贪心选模式（B → C → A），每次均执行前瞻能量校验
  Phase 3  汇总模式 A 订单，为每辆卡车构建最近邻路径（含经停充电站）
"""

from __future__ import annotations

import logging
import math
import networkx as nx
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from config.loader import load_solver_energy_params
from core.entities.primitives import DroneStatus, Position3D
from pyproj import Transformer
import osm_service as _osm_svc
import sys
import os

# 导入预设场景模块用于加载缓存的 OSM 数据
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "environment", "geo"))
try:
    from preset_scenes import load_osm_from_cache
except ImportError:
    load_osm_from_cache = None

if TYPE_CHECKING:
    from core.entities.drone import Drone
    from core.entities.order import Order
    from core.entities.swap_station import SwapStation
    from core.entities.truck import Truck
    from entity_manager import EntityManager

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AllocationResult:
    """单条订单的分配结果。"""
    order_id: str
    vehicle_id: str              # 分配给哪个卡车 / 仓库
    mode: str                    # 'A' / 'B' / 'C' / 'B_WAIT' / 'REJECT'
    distance: float              # 关键路段距离（米）
    feasible: bool
    reason: str = ""
    recovery_station_id: str = ""      # 模式 B/C：无人机完成任务后的降落点 ID
    drone_id: str = ""                  # 实际分配的无人机 ID（模式 B/C 时填写）
    # 新增字段：支持无人机在充电站等待场景
    launch_station_id: str = ""         # 模式 B_WAIT：无人机出发的充电站 ID
    launch_time: float = 0.0            # 无人机实际起飞时间
    wait_duration: float = 0.0          # 在出发充电站等待的时间（秒）
    # 统一评分结果（用于模式间比较）
    score_total: float = math.inf
    cost_dist: float = 0.0
    cost_energy: float = 0.0
    cost_penalty: float = 0.0


@dataclass
class TruckRouteNode:
    """卡车配送路径中的单个节点。"""
    node_id: str
    node_type: str               # 'depot' / 'customer' / 'station'
    position: Position3D
    arrival_time: float = 0.0
    departure_time: float = 0.0
    order_id: str = ""           # 仅 customer 节点有效


@dataclass
class TruckRoute:
    """卡车完整行驶路径（depot → customers/stations → depot）。"""
    truck_id: str
    nodes: list[TruckRouteNode] = field(default_factory=list)
    total_distance: float = 0.0
    charging_stop_ids: list[str] = field(default_factory=list)  # 经停充电站 ID，广播给无人机
    geometry: list[Position3D] = field(default_factory=list)  # 完整路径几何（UTM 坐标序列）


@dataclass
class DroneRoute:
    """无人机飞行路径（仓库/卡车 → 配送点 → 回收点）。"""
    drone_id: str
    order_id: str
    path: list[Position3D] = field(default_factory=list)  # 完整飞行路径（WGS84 坐标序列，lon/lat）
    mode: str = ""  # 'B' / 'C'
    launch_loc: Position3D = field(default_factory=lambda: Position3D(0, 0, 0))
    delivery_loc: Position3D = field(default_factory=lambda: Position3D(0, 0, 0))
    recovery_loc: Position3D = field(default_factory=lambda: Position3D(0, 0, 0))


@dataclass
class DispatchPlan:
    """全量分配方案（一个调度批次）。"""
    allocations: list[AllocationResult]
    cost_total: float
    summary: dict
    truck_routes: dict[str, TruckRoute] = field(default_factory=dict)  # truck_id → TruckRoute
    drone_routes: dict[str, DroneRoute] = field(default_factory=dict)  # drone_id → DroneRoute


# ══════════════════════════════════════════════════════════════════════════════
# 算法主体
# ══════════════════════════════════════════════════════════════════════════════

class GreedyBaseline:
    """
    贪心调度决策器 v2。

    调参常量：
      SERVICE_TIME_CUSTOMER  客户节点卸货停留时间 [s]
      MAX_DETOUR_RATIO       充电站插入可接受的绕路比（1.3 = 最多绕路 30%）
      ENERGY_SAFETY_FACTOR   能量安全系数（保留 20% 冗余）
    """

    SERVICE_TIME_CUSTOMER = 60.0   # [s]
    MAX_DETOUR_RATIO      = 1.3
    ENERGY_SAFETY_FACTOR  = 1.2
    
    # 新增参数：控制无人机在充电站等待的成本权衡
    WAIT_PENALTY_FACTOR   = 0.5    # 每秒等待时间的成本系数（相对于距离）
    EARLY_ARRIVAL_PENALTY = 120.0  # 每分钟提早到达的惩罚（相对于距离）

    # 统一目标函数系数：f = f_dist + f_energy + f_penalty
    C_DIST_ET = 1.0
    C_DIST_UAV = 1.0
    C_ENERGY_ET = 1.0
    C_ENERGY_UAV = 1.0
    LAMBDA_TIME = 1.0

    # 能耗模型参数（用于评分）
    TRUCK_ENERGY_KWH_PER_KM = 0.75  # ET: 每公里耗电 [kWh/km]
    TRUCK_ENERGY_WH_PER_METER = TRUCK_ENERGY_KWH_PER_KM  # = 0.75 Wh/m
    UAV_ALPHA_WH_PER_KG_KM = 0.24   # UAV: alpha_k [Wh/(kg·km)]

    # 业务时长参数（默认值，可由配置覆盖）
    TRUCK_DRONE_LAUNCH_TIME = 10.0
    TRUCK_DRONE_RECOVER_TIME = 10.0

    def __init__(self, entity_mgr: "EntityManager") -> None:
        self.entity_mgr = entity_mgr
        self.min_reserve_energy = 200.0   # 无人机绝对电量底线 [J]
        self.delivery_service_time = 30.0  # 配送点停留时间 [s]

        # 评分与能耗参数统一从配置读取，便于所有算法共享。
        energy_cfg = load_solver_energy_params()
        self.C_DIST_ET = energy_cfg.c_dist_et
        self.C_DIST_UAV = energy_cfg.c_dist_uav
        self.C_ENERGY_ET = energy_cfg.c_energy_et
        self.C_ENERGY_UAV = energy_cfg.c_energy_uav
        self.LAMBDA_TIME = energy_cfg.lambda_time
        self.TRUCK_ENERGY_KWH_PER_KM = energy_cfg.truck_energy_kwh_per_km
        self.TRUCK_ENERGY_WH_PER_METER = energy_cfg.truck_energy_wh_per_meter
        self.UAV_ENERGY_MODEL = energy_cfg.uav_energy_model
        self.UAV_ALPHA_WH_PER_KG_KM = energy_cfg.uav_alpha_wh_per_kg_km
        self.ALLOW_MOVING_TRUCK_LAUNCH = energy_cfg.allow_moving_truck_launch
        self.SERVICE_TIME_CUSTOMER = energy_cfg.truck_service_time_order_s
        self.delivery_service_time = energy_cfg.drone_service_time_order_s
        self.TRUCK_DRONE_LAUNCH_TIME = energy_cfg.truck_drone_launch_time_s
        self.TRUCK_DRONE_RECOVER_TIME = energy_cfg.truck_drone_recover_time_s

    # ══════════════════════════════════════════════════════════════════════════
    # 公共接口
    # ══════════════════════════════════════════════════════════════════════════

    def dispatch(
        self,
        pending_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """
        执行一轮贪心调度。

        Phase 1  按剩余时间窗升序（最紧迫的先处理）
        Phase 2  逐单选模式（B → C → A）+ 前瞻能量校验
        Phase 3  为模式 A 订单构建卡车路径（最近邻 + 充电站插入）
        
        Args:
            pending_orders: 待分配的订单字典
            current_time: 当前仿真时刻
            bbox: 地图边界
            scene_id: 预设场景 ID，如 'default_test_4x4km'（可选）
        """
        # 加载 OSM 路网：优先从缓存加载
        road_graph = None
        nodes = None
        
        if scene_id and load_osm_from_cache:
            logger.info(f"[GreedyBaseline] 尝试从预设场景 '{scene_id}' 缓存加载 OSM...")
            osm_xml, osm_geojson = load_osm_from_cache(scene_id)
            if osm_xml:
                try:
                    road_graph, nodes = _osm_svc.build_road_graph(osm_xml)
                    logger.info(f"[GreedyBaseline] 从缓存加载成功: {len(nodes)} 个节点，{len(road_graph.edges)} 条边")
                except Exception as e:
                    logger.warning(f"[GreedyBaseline] 从缓存构建失败: {e}，将重新下载")
                    road_graph = None
        
        # 如果缓存加载失败或无预设场景，则下载 OSM
        if road_graph is None:
            logger.info("[GreedyBaseline] 下载 OSM 路网数据...")
            try:
                osm_xml = _osm_svc.download_osm(bbox["minx"], bbox["miny"], bbox["maxx"], bbox["maxy"])
                road_graph, nodes = _osm_svc.build_road_graph(osm_xml)
                logger.info(f"[GreedyBaseline] 构建路网图：{len(nodes)} 个节点，{len(road_graph.edges)} 条边")
            except Exception as e:
                raise RuntimeError(f"OSM 路网下载/构建失败，已终止调度: {e}") from e

        # ── Phase 1：排序 ──────────────────────────────────────────────────
        sorted_orders = sorted(
            pending_orders.values(),
            key=lambda o: (o.deadline - current_time, o.deadline),
        )

        allocations: list[AllocationResult] = []
        mode_counter: dict[str, int] = {}
        truck_node_map: dict[str, dict] = {
            tid: {"orders": [], "recovery_stations": []}
            for tid in self.entity_mgr.trucks
        }

        # ── 追踪本次调度中已分配的无人机（防止同一个无人机被多次分配）────────
        allocated_drones: set[str] = set()

        # ── Phase 2：逐单分配 ─────────────────────────────────────────────
        for order in sorted_orders:
            result = self._allocate_order(order, current_time, allocated_drones)
            allocations.append(result)
            if result.feasible:
                mode_counter[result.mode] = mode_counter.get(result.mode, 0) + 1
                if result.mode == "A" and result.vehicle_id in truck_node_map:
                    truck_node_map[result.vehicle_id]["orders"].append(order)
                elif result.mode in ("B", "B_WAIT", "C") and result.drone_id:
                    # 记录已分配的无人机
                    allocated_drones.add(result.drone_id)
                    # B 和 B_WAIT 模式都需要添加回收点到卡车路由
                    if result.mode in ("B", "B_WAIT") and result.vehicle_id in truck_node_map:
                        truck_node_map[result.vehicle_id]["recovery_stations"].append(result.recovery_station_id)
                        # B_WAIT 模式还需要添加出发站（无人机在此等待）
                        if result.mode == "B_WAIT" and result.launch_station_id:
                            truck_node_map[result.vehicle_id]["recovery_stations"].append(result.launch_station_id)

        # ── Phase 3：构建卡车路径 ─────────────────────────────────────────
        # 构建 recovery station wait times 映射：station_id -> wait_duration
        recovery_station_wait_times: dict[str, float] = {}
        for alloc in allocations:
            if alloc.feasible and alloc.mode in ("B", "B_WAIT") and alloc.recovery_station_id:
                # 对于同一个站点被多个allocation使用的情况，取最大等待时间
                wait_s = max(0.0, alloc.wait_duration) + self.TRUCK_DRONE_RECOVER_TIME
                recovery_station_wait_times[alloc.recovery_station_id] = max(
                    recovery_station_wait_times.get(alloc.recovery_station_id, 0),
                    wait_s,
                )

        truck_routes: dict[str, TruckRoute] = {}
        for truck_id, node_data in truck_node_map.items():
            truck = self.entity_mgr.trucks[truck_id]
            # 去除 recovery_stations 中的重复
            unique_recovery_stations = list(set(node_data["recovery_stations"]))
            truck_routes[truck_id] = self._build_truck_route(
                truck, node_data["orders"], unique_recovery_stations, current_time, road_graph, nodes,
                recovery_station_wait_times,
            )

        cost_total = sum(
            r.score_total for r in allocations
            if r.feasible and math.isfinite(r.score_total)
        )

        cost_dist_total = sum(r.cost_dist for r in allocations if r.feasible)
        cost_energy_total = sum(r.cost_energy for r in allocations if r.feasible)
        cost_penalty_total = sum(r.cost_penalty for r in allocations if r.feasible)

        plan = DispatchPlan(
            allocations=allocations,
            cost_total=cost_total,
            summary={
                "total_orders": len(allocations),
                "feasible": sum(1 for r in allocations if r.feasible),
                "modes": mode_counter,
                "cost_breakdown": {
                    "dist": cost_dist_total,
                    "energy": cost_energy_total,
                    "penalty": cost_penalty_total,
                },
            },
            truck_routes=truck_routes,
        )
        logger.info("[GreedyBaseline] 分配完成：%s", plan.summary)
        return plan

    # ══════════════════════════════════════════════════════════════════════════
    # 单订单分配
    # ══════════════════════════════════════════════════════════════════════════

    def _try_mode_b_with_waiting(self, order: "Order", current_time: float, allocated_drones: set[str]) -> AllocationResult:
        """
        改进的模式 B（无人机在充电站等待）：
        无人机在卡车将要经过的充电站上等待 → 起飞 → 配送 → 回到充电站降落。

        优点：
          - 无人机在车上等待期间可被卡车供电（省电）
          - 减少无人机的单独飞行时间
          - 更符合现实的卡-空协同场景

        策略：
          1. 获取可用无人机
          2. 对每辆卡车，遍历其预测经过的充电站
          3. 从各充电站出发评估方案，选择最佳的(距离+等待成本最低)
          4. 返回全局最优方案
        """
        trucks = list(self.entity_mgr.trucks.values())
        if not trucks:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="B_WAIT",
                distance=float("inf"), feasible=False, reason="无可用卡车",
            )

        # 获取未被分配过的可用无人机
        available_drones = self._get_available_drones()
        available_drones = [d for d in available_drones if d.drone_id not in allocated_drones]
        drone = self._find_capable_drone(order.payload_weight, available_drones)
        if drone is None:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="B_WAIT",
                distance=float("inf"), feasible=False, reason="无未分配的载重匹配无人机",
            )

        recovery_pool = self._get_recovery_pool()
        best_scenario = None
        best_truck_id = None

        # 尝试每辆卡车的充电站
        for truck in trucks:
            # 预测该卡车会经过的充电站
            predicted_stations = self._predict_truck_charging_stations(truck, current_time)

            for station_id, station_loc in predicted_stations:
                truck_loc = truck.get_location(current_time)
                truck_distance_to_launch = self._dist(truck_loc, station_loc)
                launch_delay = (
                    truck_distance_to_launch / truck.speed
                    if truck.speed > 0 else float("inf")
                )

                # 评估从该充电站出发的方案
                scenario = self._evaluate_charging_station_departure(
                    drone=drone,
                    launch_loc=station_loc,
                    launch_station_id=station_id,
                    truck_distance_to_launch=truck_distance_to_launch,
                    launch_delay=launch_delay,
                    delivery_loc=order.delivery_loc,
                    payload=order.payload_weight,
                    recovery_pool=recovery_pool,
                    current_time=current_time,
                    order=order,
                )

                if scenario and scenario["feasible"]:
                    # 该方案可行，比较得分
                    if best_scenario is None or scenario["score"] < best_scenario["score"]:
                        best_scenario = scenario
                        best_truck_id = truck.truck_id

        # 返回最佳方案
        if best_scenario:
            return AllocationResult(
                order_id=order.order_id,
                vehicle_id=best_truck_id,
                mode="B_WAIT",
                distance=best_scenario["distance"],
                feasible=True,
                recovery_station_id=best_scenario["recovery_station_id"],
                drone_id=drone.drone_id,
                launch_station_id=best_scenario["launch_station_id"],
                launch_time=best_scenario["launch_time"],
                wait_duration=best_scenario["wait_duration"],
                score_total=best_scenario["score"],
                cost_dist=best_scenario["cost_dist"],
                cost_energy=best_scenario["cost_energy"],
                cost_penalty=best_scenario["cost_penalty"],
            )

        # 找不到可行方案
        return AllocationResult(
            order_id=order.order_id, vehicle_id="", mode="B_WAIT",
            distance=float("inf"), feasible=False,
            reason="无可行充电站方案（能量或距离不可行）",
        )

    # ══════════════════════════════════════════════════════════════════════════

    def _allocate_order(self, order: "Order", current_time: float, allocated_drones: set[str]) -> AllocationResult:
        """在可行候选中选择统一评分最小的方案。
        
        Args:
            order: 待分配订单
            current_time: 当前仿真时刻
            allocated_drones: 本次调度中已分配的无人机ID集合（用于防止重复分配）
        """
        candidates: list[AllocationResult] = []

        try_fns = [self._try_mode_b_with_waiting, self._try_mode_c, self._try_mode_a]
        if self.ALLOW_MOVING_TRUCK_LAUNCH:
            try_fns.insert(1, self._try_mode_b)

        for try_fn in try_fns:
            result = try_fn(order, current_time, allocated_drones)
            if not result.feasible:
                continue

            score_total, cost_dist, cost_energy, cost_penalty = self._score_allocation(
                result, order, current_time
            )
            result.score_total = score_total
            result.cost_dist = cost_dist
            result.cost_energy = cost_energy
            result.cost_penalty = cost_penalty
            candidates.append(result)

        if candidates:
            best = min(candidates, key=lambda r: r.score_total)
            logger.info(
                "[GreedyBaseline] 订单 %s 候选评分: %s | 选中=%s score=%.2f (dist=%.2f, energy=%.2f, penalty=%.2f)",
                order.order_id,
                [
                    {
                        "mode": c.mode,
                        "score": round(c.score_total, 2),
                        "dist": round(c.cost_dist, 2),
                        "energy": round(c.cost_energy, 2),
                        "penalty": round(c.cost_penalty, 2),
                    }
                    for c in candidates
                ],
                best.mode,
                best.score_total,
                best.cost_dist,
                best.cost_energy,
                best.cost_penalty,
            )
            return best

        return AllocationResult(
            order_id=order.order_id,
            vehicle_id="",
            mode="REJECT",
            distance=float("inf"),
            feasible=False,
            reason="无可用资源（无卡车、无人机或电量不足）",
        )

    def _try_mode_b(self, order: "Order", current_time: float, allocated_drones: set[str]) -> AllocationResult:
        """
        模式 B（卡-空协同）：卡车当前位置起飞 → 送达 → 最近合法回收点降落。

        前瞻能量校验：drone.battery_current ≥
          (飞到配送点能耗 + 飞到最近回收点能耗) × ENERGY_SAFETY_FACTOR
        """
        trucks = list(self.entity_mgr.trucks.values())
        if not trucks:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="B",
                distance=float("inf"), feasible=False, reason="无可用卡车",
            )

        # 获取未被分配过的可用无人机
        available_drones = self._get_available_drones()
        available_drones = [d for d in available_drones if d.drone_id not in allocated_drones]
        drone = self._find_capable_drone(
            order.payload_weight, available_drones
        )
        if drone is None:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="B",
                distance=float("inf"), feasible=False, reason="无未分配的载重匹配无人机",
            )

        recovery_pool = self._get_recovery_pool()

        # 按距离从近到远尝试卡车，找第一个能量可行的
        for truck in sorted(
            trucks,
            key=lambda t: self._dist(order.delivery_loc, t.get_location(current_time)),
        ):
            launch_loc = truck.get_location(current_time)
            recovery_id = self._check_energy_feasible(
                drone=drone,
                launch_loc=launch_loc,
                delivery_loc=order.delivery_loc,
                payload=order.payload_weight,
                recovery_pool=recovery_pool,
            )
            if recovery_id is None:
                continue

            return AllocationResult(
                order_id=order.order_id,
                vehicle_id=truck.truck_id,
                mode="B",
                distance=self._dist(launch_loc, order.delivery_loc),
                feasible=True,
                recovery_station_id=recovery_id,
                drone_id=drone.drone_id,
            )

        return AllocationResult(
            order_id=order.order_id, vehicle_id="", mode="B",
            distance=float("inf"), feasible=False,
            reason="无人机电量不足以完成送达并安全回收",
        )

    def _try_mode_c(self, order: "Order", current_time: float, allocated_drones: set[str]) -> AllocationResult:
        """
        模式 C（仓-空直递）：仓库 → 无人机 → 送达 → 仓库（往返）。

        前瞻能量校验：drone.battery_current ≥
          (仓库→配送点 + 配送点→仓库) × ENERGY_SAFETY_FACTOR
        """
        depots = list(self.entity_mgr.depots.values())
        if not depots:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="C",
                distance=float("inf"), feasible=False, reason="无可用仓库",
            )

        depot = depots[0]
        # 获取未被分配过的可用无人机
        available_drones = self._get_available_drones()
        available_drones = [d for d in available_drones if d.drone_id not in allocated_drones]
        drone = self._find_capable_drone(
            order.payload_weight, available_drones
        )
        if drone is None:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="C",
                distance=float("inf"), feasible=False, reason="无未分配的载重匹配无人机",
            )

        energy_out  = self._flight_energy(drone, depot.location, order.delivery_loc, order.payload_weight)
        energy_back = self._flight_energy(drone, order.delivery_loc, depot.location, 0.0)
        energy_needed = (energy_out + energy_back) * self.ENERGY_SAFETY_FACTOR

        if energy_needed > drone.battery_current:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="C",
                distance=float("inf"), feasible=False,
                reason=(
                    f"仓-空往返电量不足（需 {energy_needed:.0f} J，"
                    f"剩余 {drone.battery_current:.0f} J）"
                ),
            )

        return AllocationResult(
            order_id=order.order_id,
            vehicle_id=depot.depot_id,
            mode="C",
            distance=self._dist(depot.location, order.delivery_loc),
            feasible=True,
            recovery_station_id=depot.depot_id,
            drone_id=drone.drone_id,
        )

    def _try_mode_a(self, order: "Order", current_time: float, allocated_drones: set[str]) -> AllocationResult:
        """模式 A（卡车直递）：选距离最近的卡车。"""
        trucks = list(self.entity_mgr.trucks.values())
        if not trucks:
            return AllocationResult(
                order_id=order.order_id, vehicle_id="", mode="A",
                distance=float("inf"), feasible=False, reason="无可用卡车",
            )

        nearest = min(
            trucks,
            key=lambda t: self._dist(order.delivery_loc, t.get_location(current_time)),
        )
        return AllocationResult(
            order_id=order.order_id,
            vehicle_id=nearest.truck_id,
            mode="A",
            distance=self._dist(order.delivery_loc, nearest.get_location(current_time)),
            feasible=True,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 卡车路径规划（最近邻 + 充电站顺路插入）
    # ══════════════════════════════════════════════════════════════════════════

    def _build_truck_route(
        self,
        truck: "Truck",
        orders: list,
        recovery_station_ids: list[str],
        current_time: float,
        road_graph: Optional[nx.DiGraph],
        nodes: dict,
        recovery_station_wait_times: Optional[dict[str, float]] = None,
    ) -> TruckRoute:
        """
        为卡车构建配送路径（最近邻启发式）。

        流程：
          1. 从仓库出发
          2. 合并客户订单和回收点作为待访问节点
          3. 每步贪心选取最近的未访问节点
          4. 每段路径上若有充电站满足绕路比 ≤ MAX_DETOUR_RATIO，则顺路插入
             （插入的充电站 ID 写入 charging_stop_ids，广播给无人机作为回收候选）
          5. 返回仓库

        卡车到达充电站时不停留（短暂经停，无人机在此降落等待回收）。
        """
        route = TruckRoute(truck_id=truck.truck_id)
        depots   = list(self.entity_mgr.depots.values())
        depot    = depots[0] if depots else None
        stations = list(self.entity_mgr.stations.values())

        if depot:
            route.nodes.append(TruckRouteNode(
                node_id=depot.depot_id, node_type="depot",
                position=depot.location,
                arrival_time=current_time, departure_time=current_time,
            ))
            route.geometry.append(depot.location)

        # 合并待访问节点：客户订单 + 回收点
        unvisited_nodes = []
        for order in orders:
            unvisited_nodes.append({
                "type": "customer",
                "id": order.order_id,
                "pos": order.delivery_loc,
                "order_id": order.order_id,
            })
        for station_id in recovery_station_ids:
            station = self.entity_mgr.stations.get(station_id) or self.entity_mgr.depots.get(station_id)
            if station:
                unvisited_nodes.append({
                    "type": "recovery",
                    "id": station_id,
                    "pos": station.location,
                    "order_id": "",
                })

        if not unvisited_nodes:
            return route

        cur_pos    = depot.location if depot else truck.get_location(current_time)
        cur_time   = current_time
        total_dist = 0.0

        def calc_dist_and_geometry(pos1: Position3D, pos2: Position3D) -> tuple[float, list[Position3D]]:
            """计算距离并返回对应几何（UTM）；严格使用 OSM，不允许近似回退。"""
            if road_graph is None or len(road_graph.nodes()) == 0:
                raise RuntimeError("OSM 路网为空，无法进行卡车路径规划")

            start_node = _osm_svc.find_nearest_node(road_graph, nodes, pos1.x, pos1.y)
            end_node = _osm_svc.find_nearest_node(road_graph, nodes, pos2.x, pos2.y)
            if not start_node or not end_node:
                raise RuntimeError("无法将坐标映射到 OSM 路网节点")

            # 起终点吸附到同一 OSM 节点时，视为可达的局部短段（避免误判“不可达”）
            if start_node == end_node:
                return pos1.distance_2d(pos2), [pos1, pos2]

            path = _osm_svc.shortest_path(road_graph, start_node, end_node)
            if not path:
                raise RuntimeError(f"OSM 路段不可达: {start_node} -> {end_node}")

            dist = 0.0
            segment_geometry: list[Position3D] = []
            transformer = Transformer.from_crs("EPSG:4326", "EPSG:32651")
            for i in range(len(path) - 1):
                u, v = path[i], path[i+1]
                dist += road_graph[u][v]['weight']
                if not segment_geometry:
                    lon_u, lat_u = nodes[u]
                    x_u, y_u = transformer.transform(lat_u, lon_u)
                    segment_geometry.append(Position3D(x=x_u, y=y_u, z=0))
                lon_v, lat_v = nodes[v]
                x_v, y_v = transformer.transform(lat_v, lon_v)
                segment_geometry.append(Position3D(x=x_v, y=y_v, z=0))

            return dist, segment_geometry

        def calc_dist(pos1: Position3D, pos2: Position3D) -> float:
            """仅返回距离，供贪心比较与站点插入判断使用。"""
            dist, _ = calc_dist_and_geometry(pos1, pos2)
            return dist

        while unvisited_nodes:
            # 贪心：选距当前位置最近的未访问节点
            next_node = min(unvisited_nodes, key=lambda n: calc_dist(cur_pos, n["pos"]))
            unvisited_nodes.remove(next_node)
            next_pos = next_node["pos"]

            # 尝试在 cur_pos → next_pos 段顺路插入充电站
            station = self._find_insertable_station(
                cur_pos, next_pos, stations, route.charging_stop_ids, calc_dist
            )
            # 若待访问节点本身就是该充电站，不再额外插入同站 station 节点，避免重复停靠。
            if station is not None and station.station_id == next_node["id"]:
                station = None
            if station:
                s_pos    = station.location
                d_to_s, g_to_s   = calc_dist_and_geometry(cur_pos, s_pos)
                d_s_to_c, g_s_to_c = calc_dist_and_geometry(s_pos, next_pos)
                s_arrive = cur_time + d_to_s / truck.speed

                if g_to_s:
                    route.geometry.extend(g_to_s[1:] if route.geometry else g_to_s)
                if g_s_to_c:
                    route.geometry.extend(g_s_to_c[1:] if route.geometry else g_s_to_c)

                route.nodes.append(TruckRouteNode(
                    node_id=station.station_id, node_type="station",
                    position=s_pos,
                    arrival_time=s_arrive, departure_time=s_arrive,
                ))
                route.charging_stop_ids.append(station.station_id)
                total_dist += d_to_s
                cur_pos     = s_pos
                cur_time    = s_arrive
                total_dist += d_s_to_c
                arrive      = cur_time + d_s_to_c / truck.speed
            else:
                seg, g_seg = calc_dist_and_geometry(cur_pos, next_pos)
                if g_seg:
                    route.geometry.extend(g_seg[1:] if route.geometry else g_seg)
                total_dist += seg
                arrive = cur_time + seg / truck.speed

            # 添加节点
            node_type = "customer" if next_node["type"] == "customer" else "recovery"
            if node_type == "recovery" and recovery_station_wait_times:
                # 对于 recovery 节点，使用映射中的等待时间（无人机飞行往返时间）
                service_time = recovery_station_wait_times.get(next_node["id"], self.SERVICE_TIME_CUSTOMER)
            else:
                # 对于 customer 节点，使用标准配送时间
                service_time = self.SERVICE_TIME_CUSTOMER
            
            depart = arrive + service_time
            route.nodes.append(TruckRouteNode(
                node_id=next_node["id"], node_type=node_type,
                position=next_pos,
                arrival_time=arrive, departure_time=depart,
                order_id=next_node["order_id"],
            ))
            cur_pos  = next_pos
            cur_time = depart

        # 返回仓库
        if depot:
            d, g_back = calc_dist_and_geometry(cur_pos, depot.location)
            if g_back:
                route.geometry.extend(g_back[1:] if route.geometry else g_back)
            total_dist += d
            route.nodes.append(TruckRouteNode(
                node_id=depot.depot_id + "_return", node_type="depot",
                position=depot.location,
                arrival_time=cur_time + d / truck.speed,
                departure_time=cur_time + d / truck.speed,
            ))

        route.total_distance = total_dist
        logger.info(
            "[GreedyBaseline] 卡车 %s 路径：%d 节点，总里程 %.0f m，"
            "经停充电站 %d 个（%s）",
            truck.truck_id, len(route.nodes), total_dist,
            len(route.charging_stop_ids), route.charging_stop_ids,
        )
        return route

    def _find_insertable_station(
        self,
        from_pos: Position3D,
        to_pos: Position3D,
        stations: list,
        already_inserted: list,
        dist_func,
    ) -> Optional["SwapStation"]:
        """
        在 from_pos → to_pos 路段上寻找可顺路插入的充电站。

        条件：绕路距离 ≤ 直线距离 × MAX_DETOUR_RATIO，且尚未插入路径。
        Returns 额外里程最小的充电站；无满足条件的站点则返回 None。
        """
        direct = dist_func(from_pos, to_pos)
        if direct < 1.0:
            return None

        best: Optional["SwapStation"] = None
        best_extra = float("inf")

        for s in stations:
            if s.station_id in already_inserted:
                continue
            detour = dist_func(from_pos, s.location) + dist_func(s.location, to_pos)
            if detour <= direct * self.MAX_DETOUR_RATIO:
                extra = detour - direct
                if extra < best_extra:
                    best_extra, best = extra, s
        return best

    # ══════════════════════════════════════════════════════════════════════════
    # 前瞻能量校验
    # ══════════════════════════════════════════════════════════════════════════

    def _check_energy_feasible(
        self,
        drone: "Drone",
        launch_loc: Position3D,
        delivery_loc: Position3D,
        payload: float,
        recovery_pool: list,
    ) -> Optional[str]:
        """
        前瞻能量校验：验证无人机能完成送达并安全降落到某个回收点。

        改进策略：
          优先选择充电站（距离配送点最近）>其次选择仓库（距离配送点最近）
          而不是全局能量消耗最小

        约束（含安全余量）：
          (E_launch→delivery + E_delivery→recovery) × ENERGY_SAFETY_FACTOR
            ≤ drone.battery_current
        """
        energy_to_deliver = self._flight_energy(drone, launch_loc, delivery_loc, payload)

        # 分离充电站和仓库
        stations_candidates = []
        depots_candidates = []

        for node_id, node_pos in recovery_pool:
            energy_to_recovery = self._flight_energy(drone, delivery_loc, node_pos, 0.0)
            total = (energy_to_deliver + energy_to_recovery) * self.ENERGY_SAFETY_FACTOR
            if total <= drone.battery_current:
                dist = self._dist(delivery_loc, node_pos)
                if node_id in self.entity_mgr.stations:
                    stations_candidates.append((node_id, dist))
                elif node_id in self.entity_mgr.depots:
                    depots_candidates.append((node_id, dist))

        # 优先返回充电站中距离最近的
        if stations_candidates:
            best_id = min(stations_candidates, key=lambda x: x[1])[0]
            logger.info(
                "[GreedyBaseline] 回收点选择：充电站 %s（距离 %.0fm）",
                best_id, self._dist(delivery_loc, self.entity_mgr.stations[best_id].location)
            )
            return best_id

        # 其次返回仓库中距离最近的
        if depots_candidates:
            best_id = min(depots_candidates, key=lambda x: x[1])[0]
            logger.info(
                "[GreedyBaseline] 回收点选择：仓库 %s（距离 %.0fm）",
                best_id, self._dist(delivery_loc, self.entity_mgr.depots[best_id].location)
            )
            return best_id

        return None

    def _flight_energy(
        self,
        drone: "Drone",
        from_pos: Position3D,
        to_pos: Position3D,
        payload: float,
    ) -> float:
        """
        计算无人机飞行一段路程的能量消耗（焦耳）。

        公式（多旋翼功耗模型）：
          E = P(payload, v_cruise) × (distance / v_cruise)
          P = k1 × (m_empty + payload)^1.5 + k2 × v^3
        """
        distance = self._dist(from_pos, to_pos)
        if distance < 0.001:
            return 0.0
        flight_time = distance / drone.cruise_speed
        power = drone.calculate_power(payload, drone.cruise_speed)
        return power * flight_time

    # ══════════════════════════════════════════════════════════════════════════
    # 工具方法
    # ══════════════════════════════════════════════════════════════════════════

    def _get_available_drones(self) -> list:
        """返回当前空闲（IDLE）且电量高于底线的无人机列表。"""
        return [
            d for d in self.entity_mgr.drones.values()
            if d.status == DroneStatus.IDLE
            and d.battery_current > self.min_reserve_energy
        ]

    @staticmethod
    def _find_capable_drone(payload_weight: float, drones: list) -> Optional[object]:
        """
        在可用无人机中找到载重能力 ≥ payload_weight 的无人机。
        优先选择电量最充足的，以降低能量校验失败概率。
        """
        capable = [d for d in drones if d.payload_capacity >= payload_weight]
        if not capable:
            return None
        return max(capable, key=lambda d: d.battery_current)

    def _get_recovery_pool(self) -> list:
        """返回所有合法无人机降落点：充换电站 + 仓库。"""
        pool = []
        for s in self.entity_mgr.stations.values():
            pool.append((s.station_id, s.location))
        for dep in self.entity_mgr.depots.values():
            pool.append((dep.depot_id, dep.location))
        return pool

    @staticmethod
    def _dist(pos_a: Position3D, pos_b: Position3D) -> float:
        """
        二维欧氏距离（用于启发式排序/估计，不作为卡车路网主路径）。
        """
        dx = pos_a.x - pos_b.x
        dy = pos_a.y - pos_b.y
        return math.sqrt(dx * dx + dy * dy)

    @staticmethod
    def _distance_3d(pos_a: Position3D, pos_b: Position3D) -> float:
        """三维欧氏距离（保留旧名兼容外部调用）。"""
        dx = pos_a.x - pos_b.x
        dy = pos_a.y - pos_b.y
        dz = pos_a.z - pos_b.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def _truck_energy_wh(self, distance_m: float) -> float:
        """卡车电耗模型：0.75 kWh/km -> 0.75 Wh/m。"""
        return max(0.0, distance_m) * self.TRUCK_ENERGY_WH_PER_METER

    def _uav_energy_wh(
        self,
        drone: "Drone",
        from_pos: Position3D,
        to_pos: Position3D,
        payload: float,
    ) -> float:
        """
        UAV 能耗模型（评分用）：
        - physics: 复用 drone.py 既有功率模型，先算焦耳再转 Wh
        - alpha:   线性经验模型 B_ij = alpha_k * (W_k + payload) * d_ij(km)
        """
        if self.UAV_ENERGY_MODEL == "physics":
            # _flight_energy 内部已调用 drone.calculate_power(...)，与实体耗能口径一致。
            return self._flight_energy(drone, from_pos, to_pos, payload) / 3600.0

        distance_km = self._dist(from_pos, to_pos) / 1000.0
        total_mass = max(0.0, drone.empty_weight + max(0.0, payload))
        return self.UAV_ALPHA_WH_PER_KG_KM * total_mass * distance_km

    def _score_allocation(
        self,
        alloc: AllocationResult,
        order: "Order",
        current_time: float,
    ) -> tuple[float, float, float, float]:
        """
        统一评分：f = f_dist + f_energy + f_penalty。

        Returns:
            (score_total, cost_dist, cost_energy, cost_penalty)
        """
        if not alloc.feasible:
            return float("inf"), 0.0, 0.0, 0.0

        truck_distance = 0.0
        uav_distance = 0.0
        truck_energy = 0.0
        uav_energy = 0.0
        delivery_time_est = current_time

        if alloc.mode == "A":
            truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
            if truck is None or truck.speed <= 0:
                return float("inf"), 0.0, 0.0, 0.0
            truck_distance = self._dist(truck.get_location(current_time), order.delivery_loc)
            truck_energy = self._truck_energy_wh(truck_distance)
            delivery_time_est = current_time + truck_distance / truck.speed + self.SERVICE_TIME_CUSTOMER

        elif alloc.mode in ("B", "B_WAIT"):
            drone = self.entity_mgr.drones.get(alloc.drone_id)
            if drone is None or drone.cruise_speed <= 0:
                return float("inf"), 0.0, 0.0, 0.0

            if alloc.mode == "B_WAIT":
                truck = self.entity_mgr.trucks.get(alloc.vehicle_id)
                launch_station = self.entity_mgr.stations.get(alloc.launch_station_id)
                if truck is None or launch_station is None or truck.speed <= 0:
                    return float("inf"), 0.0, 0.0, 0.0
                launch_loc = launch_station.location
                truck_distance = self._dist(truck.get_location(current_time), launch_loc)
                truck_energy = self._truck_energy_wh(truck_distance)
                launch_time_est = (
                    alloc.launch_time
                    if alloc.launch_time > current_time
                    else current_time + truck_distance / truck.speed + self.TRUCK_DRONE_LAUNCH_TIME
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

        elif alloc.mode == "C":
            drone = self.entity_mgr.drones.get(alloc.drone_id)
            depot = self.entity_mgr.depots.get(alloc.vehicle_id)
            if drone is None or depot is None or drone.cruise_speed <= 0:
                return float("inf"), 0.0, 0.0, 0.0
            dist_out = self._dist(depot.location, order.delivery_loc)
            dist_back = self._dist(order.delivery_loc, depot.location)
            uav_distance = dist_out + dist_back
            uav_energy = self._uav_energy_wh(drone, depot.location, order.delivery_loc, order.payload_weight)
            uav_energy += self._uav_energy_wh(drone, order.delivery_loc, depot.location, 0.0)
            delivery_time_est = current_time + dist_out / drone.cruise_speed + self.delivery_service_time

        else:
            return float("inf"), 0.0, 0.0, 0.0

        cost_dist = self.C_DIST_ET * truck_distance + self.C_DIST_UAV * uav_distance
        cost_energy = self.C_ENERGY_ET * truck_energy + self.C_ENERGY_UAV * uav_energy
        lateness = max(0.0, delivery_time_est - order.deadline)
        cost_penalty = self.LAMBDA_TIME * order.penalty_rate * lateness
        score_total = cost_dist + cost_energy + cost_penalty
        return score_total, cost_dist, cost_energy, cost_penalty

    # ══════════════════════════════════════════════════════════════════════════
    # 改进的模式 B：支持无人机在充电站等待
    # ══════════════════════════════════════════════════════════════════════════

    def _evaluate_charging_station_departure(
        self,
        drone: "Drone",
        launch_loc: Position3D,
        launch_station_id: str,
        truck_distance_to_launch: float,
        launch_delay: float,
        delivery_loc: Position3D,
        payload: float,
        recovery_pool: list,
        current_time: float,
        order: "Order",
    ) -> Optional[dict]:
        """
        评估从指定充电站出发的无人机方案。

        计算能量可行、时间可行、成本最低的回收点组合。
        返回 dict 包含计分信息；若无可行方案返回 None。

        Args:
            drone: 无人机对象
            launch_loc: 充电站位置（出发点）
            launch_station_id: 充电站 ID
            delivery_loc: 配送点位置
            payload: 载重 [kg]
            recovery_pool: 回收候选点列表 [(station_id, position), ...]
            current_time: 当前仿真时间 [s]
            order: 订单对象

        Returns:
            {
                'feasible': bool,
                'launch_station_id': str,
                'launch_time': float,
                'recovery_station_id': str,
                'wait_duration': float,
                'score': float,
                'distance': float,
            }
            或 None if 无可行方案
        """
        energy_to_deliver = self._flight_energy(drone, launch_loc, delivery_loc, payload)

        best_scenario = None
        best_score = float("inf")

        for recovery_id, recovery_loc in recovery_pool:
            # 能量校验（可行性约束继续使用物理模型[J]）
            energy_to_recovery_j = self._flight_energy(drone, delivery_loc, recovery_loc, 0.0)
            total_energy_j = (energy_to_deliver + energy_to_recovery_j) * self.ENERGY_SAFETY_FACTOR

            if total_energy_j > drone.battery_current:
                continue

            # 时间计算（这里的 wait_duration 暂设为 0，实际由卡车路径决定）
            dist_out = self._dist(launch_loc, delivery_loc)
            dist_back = self._dist(delivery_loc, recovery_loc)
            total_distance = dist_out + dist_back

            # 估算飞行时间
            flight_time_out = dist_out / drone.cruise_speed if drone.cruise_speed > 0 else 1000
            flight_time_back = dist_back / drone.cruise_speed if drone.cruise_speed > 0 else 1000
            flight_time_total = flight_time_out + self.delivery_service_time + flight_time_back
            launch_operation_time = self.TRUCK_DRONE_LAUNCH_TIME

            # 统一评分：f = f_dist + f_energy + f_penalty
            delivery_time_est = (
                current_time
                + launch_delay
                + launch_operation_time
                + flight_time_out
                + self.delivery_service_time
            )
            lateness = max(0.0, delivery_time_est - order.deadline)

            cost_dist = (
                self.C_DIST_UAV * total_distance
                + self.C_DIST_ET * truck_distance_to_launch
            )
            cost_energy = (
                self.C_ENERGY_UAV * (
                    self._uav_energy_wh(drone, launch_loc, delivery_loc, payload)
                    + self._uav_energy_wh(drone, delivery_loc, recovery_loc, 0.0)
                )
                + self.C_ENERGY_ET * self._truck_energy_wh(truck_distance_to_launch)
            )
            cost_penalty = self.LAMBDA_TIME * order.penalty_rate * lateness
            score = cost_dist + cost_energy + cost_penalty

            if score < best_score:
                best_score = score
                best_scenario = {
                    'feasible': True,
                    'launch_station_id': launch_station_id,
                    'launch_time': current_time + launch_delay + launch_operation_time,
                    'recovery_station_id': recovery_id,
                    'wait_duration': launch_operation_time + flight_time_total,
                    'score': best_score,
                    'distance': total_distance,
                    'energy_needed': total_energy_j,
                    'flight_time': flight_time_total,
                    'cost_dist': cost_dist,
                    'cost_energy': cost_energy,
                    'cost_penalty': cost_penalty,
                }

        return best_scenario

    def _predict_truck_charging_stations(
        self,
        truck: "Truck",
        current_time: float,
    ) -> list[tuple[str, Position3D]]:
        """
        快速预测卡车将经过的充电站（基于当前位置和已有的快速贪心预测）。

        此方法返回卡车路线上按顺序出现的充电站列表。

        Args:
            truck: 卡车对象
            current_time: 当前仿真时间

        Returns:
            [(station_id, station_location), ...] 按卡车轨迹顺序
        """
        stations = list(self.entity_mgr.stations.values())
        if not stations:
            return []

        # 简单启发式：返回离卡车当前位置最近的 3 个充电站
        # （在实际应用中应基于完整的卡车路由预测）
        truck_loc = truck.get_location(current_time)
        nearby_stations = sorted(
            stations,
            key=lambda s: self._dist(truck_loc, s.location),
        )[:3]

        return [(s.station_id, s.location) for s in nearby_stations]
