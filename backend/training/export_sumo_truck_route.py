#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — Phase 4 卡车路线导出与 SUMO 验证产物生成。

设计原则：
  - 区分卡车完整执行路线与 `truck_backbone_route`；
  - 完整执行路线包含 `depot / customer / station`；
  - `truck_backbone_route` 仅保留未来固定节点（`station / depot`）；
  - 订单访问顺序：deadline 为主，OSM 路网最短路径距离为 tie-break；
  - 充换电站插入：以路网绕路代价最小为准，沿路站点绕路代价自然接近 0；
  - ETA 使用 OSM 实际可达路径长度 / `truck.speed` 计算，不使用曼哈顿距离。

执行流程：
  1. 加载场景（depot / truck / orders / stations / OSM 路网）；
  2. 构建 OSM 路网图（DiGraph）与 SUMO net 中间产物——必须在规划前完成；
  3. 按 deadline + 路网距离贪心排序订单；
  4. 插入充换电站，满足 min_future_fixed_nodes 约束；
  5. 物化执行路线：对每段 stop-to-stop 跑 Dijkstra，映射到 SUMO edge 序列；
  6. 导出 net.xml / rou.xml / poi.add.xml / sumocfg 及各 JSON 产物。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence



REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
GEO_DIR = BACKEND_DIR / "environment" / "geo"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(GEO_DIR) not in sys.path:
    sys.path.insert(0, str(GEO_DIR))


from core.entities.order import Order
from core.entities.primitives import Position3D
from config.loader import load_drone_params, load_solver_energy_params
from environment.geo.exporters.sumo_net_osm import (
    build_sumo_net_artifacts,
    write_sumo_net_artifacts,
)
from environment.geo.osm_service import build_road_graph, find_nearest_node, shortest_path, shortest_path_length
from training.scene_loader import DEFAULT_CONFIG_PATH, TrainingSceneContext, load_default_scene


DEFAULT_OUTPUT_SUBDIR = Path("sumo") / "phase4_truck_route"
SNAP_WARN_THRESHOLD_M = 60.0
# 绕路代价低于此值视为"顺路"，无论数量多少都直接加入路线。
STATION_ONROUTE_DETOUR_THRESHOLD_M = 100.0

_DRONE_PARAMS = load_drone_params()
_SOLVER_ENERGY_PARAMS = load_solver_energy_params()
TRUCK_CUSTOMER_SERVICE_TIME_SEC = float(_SOLVER_ENERGY_PARAMS.truck_service_time_order_s)
TRUCK_STATION_HOLD_TIME_SEC = max(
    float(_SOLVER_ENERGY_PARAMS.truck_drone_launch_time_s),
    float(_SOLVER_ENERGY_PARAMS.truck_drone_recover_time_s),
)


# 超过此重量的订单无人机无法配送，由卡车直送。
HEAVY_DRONE_PAYLOAD_CAPACITY_KG: float = float(_DRONE_PARAMS.heavy.payload_capacity)


@dataclass(frozen=True)
class Phase4TruckOrder:
    order: Order
    raw: Mapping[str, Any]
    deadline: float
    fulfillment_mode: str


@dataclass(frozen=True)
class RouteStop:
    seq: int
    node_type: str
    node_id: str
    x: float
    y: float
    z: float
    order_id: str | None
    arrival_time_sec: float
    departure_time_sec: float
    nearest_osm_node_id: str
    snap_distance_m: float

    @property
    def position(self) -> Position3D:
        return Position3D(x=self.x, y=self.y, z=self.z)


@dataclass(frozen=True)
class RouteSegment:
    from_node_id: str
    to_node_id: str
    from_node_type: str
    to_node_type: str
    distance_m: float
    travel_time_sec: float
    osm_node_path: tuple[str, ...]
    sumo_edge_ids: tuple[str, ...]


@dataclass(frozen=True)
class TruckExecutionRoute:
    truck_id: str
    stops: tuple[RouteStop, ...]
    segments: tuple[RouteSegment, ...]
    total_distance_m: float
    total_travel_time_sec: float
    sumo_edge_sequence: tuple[str, ...]


@dataclass(frozen=True)
class Phase4ExportResult:
    execution_route: TruckExecutionRoute
    truck_backbone_route: tuple[str, ...]
    truck_eta_map: Mapping[str, float]
    route_drift_ref: Mapping[str, Mapping[str, float | int]]
    validation_report: Mapping[str, Any]


def export_phase4_truck_route(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    output_dir: str | Path | None = None,
    min_future_fixed_nodes: int | None = None,
) -> Phase4ExportResult:
    config_path = Path(config_path)
    resolved_min_future_fixed_nodes = _resolve_min_future_fixed_nodes(
        config_path=config_path,
        override=min_future_fixed_nodes,
    )
    scene_ctx = load_default_scene(config_path=config_path)
    export_dir = _resolve_output_dir(scene_ctx, output_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    depot_id, depot = _require_singleton(scene_ctx.depots, "depot")
    truck_id, truck = _require_singleton(scene_ctx.trucks, "truck")

    osm_xml_path = scene_ctx.road_network.xml_path
    if not osm_xml_path:
        raise ValueError("scene_loader 未提供 osm_network.xml 路径，无法执行 Phase 4")
    osm_xml = Path(osm_xml_path).read_text(encoding="utf-8")
    road_graph, road_nodes = build_road_graph(osm_xml, respect_osm_oneway=True)
    net_artifacts = build_sumo_net_artifacts(
        osm_xml,
        _scene_bounds_tuple(scene_ctx.bounds),
    )

    truck_orders = _select_truck_orders(scene_ctx)
    ordered_orders = _order_truck_orders(truck_orders, depot.location, road_graph, road_nodes)
    initial_execution_plan = _build_initial_execution_plan(depot_id, depot.location, ordered_orders)
    execution_plan, inserted_fixed_nodes = _ensure_min_future_fixed_nodes(
        initial_execution_plan,
        stations=scene_ctx.stations,
        min_future_fixed_nodes=resolved_min_future_fixed_nodes,
        road_graph=road_graph,
        road_nodes=road_nodes,
    )

    execution_route = _materialize_execution_route(
        truck_id=truck_id,
        truck_speed=float(truck.speed),
        execution_plan=execution_plan,
        road_graph=road_graph,
        road_nodes=road_nodes,
        directed_step_to_edge=net_artifacts.directed_step_to_edge,
    )
    truck_backbone_route = _project_backbone_route(execution_route)
    truck_eta_map = _build_truck_eta_map(execution_route, truck_backbone_route)
    route_drift_ref = _build_route_drift_ref(truck_backbone_route, truck_eta_map)

    net_path = export_dir / "truck_route.net.xml"
    write_sumo_net_artifacts(net_artifacts, str(net_path))
    _write_poi_file(
        scene_ctx=scene_ctx,
        execution_route=execution_route,
        inserted_fixed_nodes=inserted_fixed_nodes,
        road_nodes=road_nodes,
        net_artifacts=net_artifacts,
        output_path=export_dir / "poi.add.xml",
    )
    _write_route_file(execution_route, export_dir / "truck_route.rou.xml")
    _write_sumocfg(
        execution_route=execution_route,
        output_path=export_dir / "truck_route.sumocfg",
    )
    _write_gui_settings(export_dir / "phase4_gui.view.xml")
    _write_json(
        export_dir / "truck_execution_route.json",
        _build_execution_route_payload(execution_route),
    )
    _write_json(
        export_dir / "truck_backbone_route.json",
        {
            "truck_id": truck_id,
            "truck_backbone_route": list(truck_backbone_route),
        },
    )
    _write_json(export_dir / "truck_eta_map.json", dict(truck_eta_map))
    _write_json(export_dir / "route_drift_ref.json", route_drift_ref)
    _write_json(
        export_dir / "phase4_debug_trace.json",
        _build_phase4_debug_trace(
            truck_id=truck_id,
            ordered_orders=ordered_orders,
            initial_execution_plan=initial_execution_plan,
            execution_plan=execution_plan,
            inserted_fixed_nodes=inserted_fixed_nodes,
            execution_route=execution_route,
            min_future_fixed_nodes=resolved_min_future_fixed_nodes,
        ),
    )

    validation_report = _build_validation_report(
        scene_ctx=scene_ctx,
        truck_orders=ordered_orders,
        execution_route=execution_route,
        truck_backbone_route=truck_backbone_route,
        truck_eta_map=truck_eta_map,
        min_future_fixed_nodes=resolved_min_future_fixed_nodes,
        export_dir=export_dir,
    )
    _write_json(export_dir / "validation_report.json", validation_report)
    validation_report = {
        **validation_report,
        "generated_files": sorted(path.name for path in export_dir.iterdir() if path.is_file()),
    }
    _write_json(export_dir / "validation_report.json", validation_report)

    return Phase4ExportResult(
        execution_route=execution_route,
        truck_backbone_route=truck_backbone_route,
        truck_eta_map=truck_eta_map,
        route_drift_ref=route_drift_ref,
        validation_report=validation_report,
    )


def _resolve_min_future_fixed_nodes(
    *,
    config_path: str | Path,
    override: int | None,
) -> int:
    if override is not None:
        return int(override)

    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "缺少 PyYAML 依赖，无法读取 rh_alns_cmrappo.yaml"
        ) from exc

    with Path(config_path).open("r", encoding="utf-8") as fh:
        raw_cfg = yaml.safe_load(fh)
    if not isinstance(raw_cfg, Mapping):
        raise ValueError(f"YAML 顶层必须为对象: {config_path}")

    planner_cfg = raw_cfg.get("planner")
    if not isinstance(planner_cfg, Mapping):
        raise ValueError(f"配置缺少 planner 段: {config_path}")

    value = int(planner_cfg.get("phase4_min_future_fixed_nodes", 2))
    if value < 1:
        raise ValueError("planner.phase4_min_future_fixed_nodes 必须 >= 1")
    return value


def _resolve_output_dir(
    scene_ctx: TrainingSceneContext,
    output_dir: str | Path | None,
) -> Path:
    if output_dir is None:
        return Path(scene_ctx.scene_bundle_dir) / DEFAULT_OUTPUT_SUBDIR
    path = Path(output_dir)
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _require_singleton(
    mapping: Mapping[str, Any],
    label: str,
) -> tuple[str, Any]:
    if len(mapping) != 1:
        raise ValueError(f"Phase 4 当前仅支持单 {label} 场景，实际数量={len(mapping)}")
    return next(iter(mapping.items()))


def _select_truck_orders(scene_ctx: TrainingSceneContext) -> tuple[Phase4TruckOrder, ...]:
    static_raw = scene_ctx.orders_raw.get("static_orders", [])
    order_by_id = {order.order_id: order for order in scene_ctx.static_orders}

    truck_orders = []
    for entry in static_raw:
        order_id = str(entry["order_id"])
        order = order_by_id.get(order_id)
        if order is None:
            raise ValueError(f"scene_loader 未找到静态订单对象: {order_id}")
        # 超过 HeavyDrone 载重上限的订单无人机无法配送，由卡车直送。
        if float(order.payload_weight) <= HEAVY_DRONE_PAYLOAD_CAPACITY_KG:
            continue
        fulfillment_mode = str(entry.get("fulfillment_mode", "")).upper()
        truck_orders.append(
            Phase4TruckOrder(
                order=order,
                raw=dict(entry),
                deadline=float(order.deadline),
                fulfillment_mode=fulfillment_mode,
            )
        )

    if not truck_orders:
        raise ValueError(
            f"未找到重量超过 HeavyDrone 上限（{HEAVY_DRONE_PAYLOAD_CAPACITY_KG} kg）的 static_orders，"
            "无法生成 Phase 4 路线"
        )
    return tuple(truck_orders)


def _order_truck_orders(
    truck_orders: Sequence[Phase4TruckOrder],
    depot_pos: Position3D,
    road_graph: Any,
    road_nodes: Mapping[str, tuple[float, float]],
) -> tuple[Phase4TruckOrder, ...]:
    # 先按 deadline 分组，组内用路网最短距离贪心选最近的下一个订单。
    # 使用路网距离而非曼哈顿距离，确保排序结果与实际行驶路线一致。
    grouped: dict[float, list[Phase4TruckOrder]] = {}
    for item in truck_orders:
        grouped.setdefault(item.deadline, []).append(item)

    ordered = []
    current_pos = depot_pos
    for deadline in sorted(grouped):
        pending = list(grouped[deadline])
        while pending:
            next_item = min(
                pending,
                key=lambda item: (
                    _road_distance(road_graph, road_nodes, current_pos, item.order.delivery_loc),
                    item.order.order_id,
                ),
            )
            ordered.append(next_item)
            current_pos = next_item.order.delivery_loc
            pending.remove(next_item)
    return tuple(ordered)


def _build_initial_execution_plan(
    depot_id: str,
    depot_pos: Position3D,
    truck_orders: Sequence[Phase4TruckOrder],
) -> list[dict[str, Any]]:
    plan = [
        _make_plan_stop("depot", depot_id, depot_pos),
    ]
    for item in truck_orders:
        plan.append(
            _make_plan_stop(
                node_type="customer",
                node_id=item.order.order_id,
                position=item.order.delivery_loc,
                order_id=item.order.order_id,
            )
        )
    plan.append(_make_plan_stop("depot", depot_id, depot_pos))
    return plan


def _ensure_min_future_fixed_nodes(
    execution_plan: list[dict[str, Any]],
    *,
    stations: Mapping[str, Any],
    min_future_fixed_nodes: int,
    road_graph: Any,
    road_nodes: Mapping[str, tuple[float, float]],
) -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    # 两阶段插入：
    #   Phase 1 — 把所有绕路代价 < STATION_ONROUTE_DETOUR_THRESHOLD_M 的站点全部加入，
    #             这些站点本来就顺路，不应跳过。每次插入后重新计算，因为计划结构已变。
    #   Phase 2 — 若 Phase 1 结束后 station 数量仍不足 min_future_fixed_nodes，
    #             按绕路代价从小到大继续补足，直到满足约束。
    if min_future_fixed_nodes < 1:
        raise ValueError("min_future_fixed_nodes 必须 >= 1")

    plan = list(execution_plan)
    inserted: list[dict[str, Any]] = []
    used_station_ids = {stop["node_id"] for stop in plan if stop["node_type"] == "station"}

    def _best_insertion(station_pos: Position3D) -> tuple[float, int] | tuple[None, None]:
        best_cost: float | None = None
        best_index: int | None = None
        for insert_at in range(1, len(plan)):
            cost = (
                _road_distance(road_graph, road_nodes, plan[insert_at - 1]["position"], station_pos)
                + _road_distance(road_graph, road_nodes, station_pos, plan[insert_at]["position"])
                - _road_distance(road_graph, road_nodes, plan[insert_at - 1]["position"], plan[insert_at]["position"])
            )
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_index = insert_at
        return best_cost, best_index

    def _do_insert(station: Any, insert_index: int, detour_cost: float, reason: str) -> None:
        plan.insert(insert_index, _make_plan_stop("station", station.station_id, station.location))
        inserted.append(
            {
                "node_type": "station",
                "node_id": station.station_id,
                "insert_index": insert_index,
                "reason": reason,
                "detour_cost_road_m": float(detour_cost),
            }
        )
        used_station_ids.add(station.station_id)

    # Phase 1: 顺路站点全部加入
    changed = True
    while changed:
        changed = False
        candidates = []
        for station_id, station in stations.items():
            if station_id in used_station_ids:
                continue
            cost, idx = _best_insertion(station.location)
            if cost is not None and cost < STATION_ONROUTE_DETOUR_THRESHOLD_M:
                candidates.append((cost, station_id, station, idx))
        if candidates:
            candidates.sort(key=lambda c: (c[0], c[1]))
            cost, _, station, idx = candidates[0]
            _do_insert(station, idx, cost, "on_route")
            changed = True

    # Phase 2: 数量不足时按绕路代价从小到大补足
    while _count_future_fixed_nodes(plan) < min_future_fixed_nodes:
        candidates = []
        for station_id, station in stations.items():
            if station_id in used_station_ids:
                continue
            cost, idx = _best_insertion(station.location)
            if cost is not None:
                candidates.append((cost, station_id, station, idx))
        if not candidates:
            raise ValueError("没有可插入的 station，无法满足最少 recovery 节点约束")
        candidates.sort(key=lambda c: (c[0], c[1]))
        cost, _, station, idx = candidates[0]
        _do_insert(station, idx, cost, "min_future_fixed_nodes")

    return plan, tuple(inserted)


def _make_plan_stop(
    node_type: str,
    node_id: str,
    position: Position3D,
    order_id: str | None = None,
) -> dict[str, Any]:
    return {
        "node_type": node_type,
        "node_id": node_id,
        "position": position,
        "order_id": order_id,
    }


def _count_future_fixed_nodes(execution_plan: Sequence[Mapping[str, Any]]) -> int:
    # min_future_fixed_nodes 只计 station，不含 depot。
    # depot 是终点保底节点，不应被算作"可插入的 recovery 节点"数量。
    return sum(1 for stop in execution_plan[1:] if stop["node_type"] == "station")


def _materialize_execution_route(
    *,
    truck_id: str,
    truck_speed: float,
    execution_plan: Sequence[Mapping[str, Any]],
    road_graph: Any,
    road_nodes: Mapping[str, tuple[float, float]],
    directed_step_to_edge: Mapping[tuple[str, str], str],
) -> TruckExecutionRoute:
    # 对每段 stop-to-stop 跑 Dijkstra，得到 OSM 节点路径，
    # 再通过 directed_step_to_edge 映射成 SUMO edge 序列，同时累计 ETA。
    if truck_speed <= 0:
        raise ValueError(f"truck.speed 必须为正数: {truck_speed}")

    nearest_cache: dict[tuple[float, float], tuple[str, float]] = {}
    route_stops: list[RouteStop] = []
    current_time = 0.0

    for seq, stop in enumerate(execution_plan):
        pos = stop["position"]
        nearest_osm_node_id, snap_distance = _find_nearest_node_cached(
            road_graph=road_graph,
            road_nodes=road_nodes,
            position=pos,
            cache=nearest_cache,
        )
        route_stops.append(
            RouteStop(
                seq=seq,
                node_type=str(stop["node_type"]),
                node_id=str(stop["node_id"]),
                x=float(pos.x),
                y=float(pos.y),
                z=float(pos.z),
                order_id=stop.get("order_id"),
                arrival_time_sec=current_time,
                departure_time_sec=current_time,
                nearest_osm_node_id=nearest_osm_node_id,
                snap_distance_m=snap_distance,
            )
        )

    segments: list[RouteSegment] = []
    sumo_edges: list[str] = []
    total_distance = 0.0

    for idx in range(1, len(route_stops)):
        prev_stop = route_stops[idx - 1]
        cur_stop = route_stops[idx]
        path = shortest_path(
            road_graph,
            prev_stop.nearest_osm_node_id,
            cur_stop.nearest_osm_node_id,
        )
        if prev_stop.nearest_osm_node_id == cur_stop.nearest_osm_node_id:
            path = [prev_stop.nearest_osm_node_id]
        elif not path:
            raise ValueError(
                f"OSM 路网不可达: {prev_stop.node_id} -> {cur_stop.node_id}"
            )

        segment_distance = _segment_distance_with_snap(prev_stop, cur_stop, path, road_nodes)
        travel_time = segment_distance / truck_speed
        arrival_time = route_stops[idx - 1].departure_time_sec + travel_time
        departure_time = arrival_time + _stop_service_time_sec(cur_stop.node_type)

        updated_cur = RouteStop(
            seq=cur_stop.seq,
            node_type=cur_stop.node_type,
            node_id=cur_stop.node_id,
            x=cur_stop.x,
            y=cur_stop.y,
            z=cur_stop.z,
            order_id=cur_stop.order_id,
            arrival_time_sec=arrival_time,
            departure_time_sec=departure_time,
            nearest_osm_node_id=cur_stop.nearest_osm_node_id,
            snap_distance_m=cur_stop.snap_distance_m,
        )
        route_stops[idx] = updated_cur

        segment_edge_ids = _osm_path_to_sumo_edges(path, directed_step_to_edge)
        sumo_edges.extend(edge for edge in segment_edge_ids if not sumo_edges or sumo_edges[-1] != edge)
        segments.append(
            RouteSegment(
                from_node_id=prev_stop.node_id,
                to_node_id=updated_cur.node_id,
                from_node_type=prev_stop.node_type,
                to_node_type=updated_cur.node_type,
                distance_m=segment_distance,
                travel_time_sec=travel_time,
                osm_node_path=tuple(path),
                sumo_edge_ids=segment_edge_ids,
            )
        )
        total_distance += segment_distance

    return TruckExecutionRoute(
        truck_id=truck_id,
        stops=tuple(route_stops),
        segments=tuple(segments),
        total_distance_m=total_distance,
        total_travel_time_sec=sum(segment.travel_time_sec for segment in segments),
        sumo_edge_sequence=tuple(sumo_edges),
    )


def _stop_service_time_sec(node_type: str) -> float:
    if node_type == "customer":
        return TRUCK_CUSTOMER_SERVICE_TIME_SEC
    if node_type == "station":
        return TRUCK_STATION_HOLD_TIME_SEC
    return 0.0


def _find_nearest_node_cached(
    *,
    road_graph: Any,
    road_nodes: Mapping[str, tuple[float, float]],
    position: Position3D,
    cache: dict[tuple[float, float], tuple[str, float]],
) -> tuple[str, float]:
    key = (round(position.x, 2), round(position.y, 2))
    cached = cache.get(key)
    if cached is not None:
        return cached

    nearest_osm_node_id = find_nearest_node(road_graph, road_nodes, position.x, position.y)
    if not nearest_osm_node_id:
        raise ValueError(f"无法将坐标映射到 OSM 节点: ({position.x}, {position.y})")
    nearest_pos = _osm_node_position(road_nodes, nearest_osm_node_id)
    snap_distance = position.distance_2d(nearest_pos)
    cache[key] = (nearest_osm_node_id, snap_distance)
    return cache[key]


def _segment_distance_with_snap(
    from_stop: RouteStop,
    to_stop: RouteStop,
    path: Sequence[str],
    road_nodes: Mapping[str, tuple[float, float]],
) -> float:
    # 路段距离 = snap 到起点 + OSM 路径折线 + snap 到终点，
    # 避免纯用 OSM 节点距离时忽略 stop 坐标与最近节点之间的偏移。
    geometry = [from_stop.position]
    for osm_node_id in path:
        pos = _osm_node_position(road_nodes, osm_node_id)
        if geometry[-1].distance_2d(pos) > 0.5:
            geometry.append(pos)
    if geometry[-1].distance_2d(to_stop.position) > 0.5:
        geometry.append(to_stop.position)
    return sum(geometry[i - 1].distance_2d(geometry[i]) for i in range(1, len(geometry)))


def _osm_node_position(
    road_nodes: Mapping[str, tuple[float, float]],
    osm_node_id: str,
) -> Position3D:
    from pyproj import Transformer

    lon, lat = road_nodes[osm_node_id]
    tr = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)
    x, y = tr.transform(lon, lat)
    return Position3D(x=float(x), y=float(y), z=0.0)


def _osm_path_to_sumo_edges(
    osm_path: Sequence[str],
    directed_step_to_edge: Mapping[tuple[str, str], str],
) -> tuple[str, ...]:
    if len(osm_path) < 2:
        return ()
    edges = []
    for idx in range(len(osm_path) - 1):
        step = (osm_path[idx], osm_path[idx + 1])
        edge_id = directed_step_to_edge.get(step)
        if edge_id is None:
            raise ValueError(f"OSM step 无法映射到 SUMO edge: {step[0]} -> {step[1]}")
        if not edges or edges[-1] != edge_id:
            edges.append(edge_id)
    return tuple(edges)


def _project_backbone_route(execution_route: TruckExecutionRoute) -> tuple[str, ...]:
    # 从完整执行路线中提取 station + depot 节点，作为 PPO 训练的骨架路线。
    # 不允许重复节点，depot 回程只取一次。
    route = []
    for stop in execution_route.stops[1:]:
        if stop.node_type not in {"station", "depot"}:
            continue
        if stop.node_id in route:
            raise ValueError(
                f"truck_backbone_route 不允许重复固定节点，当前重复节点={stop.node_id}"
            )
        route.append(stop.node_id)
    if not route:
        raise ValueError("truck_backbone_route 不能为空")
    return tuple(route)


def _build_truck_eta_map(
    execution_route: TruckExecutionRoute,
    truck_backbone_route: Sequence[str],
) -> dict[str, float]:
    eta_map = {}
    for node_id in truck_backbone_route:
        for stop in execution_route.stops[1:]:
            if stop.node_id == node_id and stop.node_type in {"station", "depot"}:
                eta_map[node_id] = stop.arrival_time_sec
                break
    missing = set(truck_backbone_route) - set(eta_map)
    if missing:
        raise ValueError(f"无法为骨架节点构建 ETA: {sorted(missing)}")
    return eta_map


def _build_route_drift_ref(
    truck_backbone_route: Sequence[str],
    truck_eta_map: Mapping[str, float],
) -> dict[str, dict[str, float | int]]:
    return {
        node_id: {
            "eta_ref": float(truck_eta_map[node_id]),
            "route_index_ref": route_index,
        }
        for route_index, node_id in enumerate(truck_backbone_route)
    }


def _write_poi_file(
    *,
    scene_ctx: TrainingSceneContext,
    execution_route: TruckExecutionRoute,
    inserted_fixed_nodes: Sequence[Mapping[str, Any]],
    road_nodes: Mapping[str, tuple[float, float]],
    net_artifacts: Any,
    output_path: Path,
) -> None:
    def local_xy(position: Position3D) -> tuple[float, float]:
        return position.x - net_artifacts.ox, position.y - net_artifacts.oy

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<additional xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '           xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/additional_file.xsd">',
    ]

    route_points: list[tuple[float, float]] = []
    for segment in execution_route.segments:
        for osm_node_id in segment.osm_node_path:
            pos = _osm_node_position(road_nodes, osm_node_id)
            point = local_xy(pos)
            if not route_points or route_points[-1] != point:
                route_points.append(point)
    if len(route_points) >= 2:
        shape = " ".join(f"{x:.2f},{y:.2f}" for x, y in route_points)
        lines.append(
            '    <poly id="TRK-TEST-01-route" type="truck_route"'
            ' color="255,96,0,180" fill="false" layer="7" lineWidth="6"'
            f' shape="{shape}"/>'
        )

    selected_fixed_nodes = {
        stop.node_id
        for stop in execution_route.stops
        if stop.node_type in {"depot", "station"}
    }
    inserted_station_ids = {
        str(item["node_id"])
        for item in inserted_fixed_nodes
        if item.get("node_type") == "station"
    }

    for depot_id, depot in scene_ctx.depots.items():
        x, y = local_xy(depot.location)
        lines.append(
            _format_marker_poly_xml(
                marker_id=f"{depot_id}-marker",
                x=x,
                y=y,
                radius=95.0,
                color="0,128,255,255",
                marker_type="selected_depot",
                layer=12,
            )
        )
        lines.append(
            _format_poi_xml(
                poi_id=depot_id,
                x=x,
                y=y,
                color="0,128,255,255",
                poi_type="depot",
                label=f"DEPOT:{depot_id}",
            )
        )
    for station_id, station in scene_ctx.stations.items():
        x, y = local_xy(station.location)
        selected = station_id in selected_fixed_nodes
        lines.append(
            _format_marker_poly_xml(
                marker_id=f"{station_id}-marker",
                x=x,
                y=y,
                radius=80.0 if selected else 58.0,
                color="0,180,0,255" if selected else "0,180,0,55",
                marker_type="selected_station" if selected else "unselected_station",
                layer=11 if selected else 5,
            )
        )
        lines.append(
            _format_poi_xml(
                poi_id=station_id,
                x=x,
                y=y,
                color="0,180,0,255" if selected else "0,180,0,55",
                poi_type="station",
                label=(
                    f"ADDED:{station_id}"
                    if station_id in inserted_station_ids
                    else f"VISITED:{station_id}"
                    if selected
                    else f"STATION:{station_id}"
                ),
            )
        )
    for stop in execution_route.stops:
        if stop.node_type != "customer":
            continue
        x, y = local_xy(stop.position)
        lines.append(
            _format_marker_poly_xml(
                marker_id=f"{stop.node_id}-marker",
                x=x,
                y=y,
                radius=72.0,
                color="255,64,64,255",
                marker_type="order",
                layer=12,
            )
        )
        lines.append(
            _format_poi_xml(
                poi_id=stop.node_id,
                x=x,
                y=y,
                color="255,64,64,255",
                poi_type="order",
                label=f"ORDER:{stop.node_id}",
            )
        )
    lines.append("</additional>")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _format_poi_xml(
    *,
    poi_id: str,
    x: float,
    y: float,
    color: str,
    poi_type: str,
    label: str | None = None,
) -> str:
    if label is None:
        return (
            f'    <poi id="{poi_id}" type="{poi_type}" color="{color}" '
            f'x="{x:.2f}" y="{y:.2f}" layer="10"/>'
        )
    return (
        f'    <poi id="{poi_id}" type="{poi_type}" color="{color}" '
        f'x="{x:.2f}" y="{y:.2f}" layer="10">\n'
        f'        <param key="PARAM_TEXT" value="{label}"/>\n'
        "    </poi>"
    )


def _format_marker_poly_xml(
    *,
    marker_id: str,
    x: float,
    y: float,
    radius: float,
    color: str,
    marker_type: str,
    layer: int,
) -> str:
    points = []
    for idx in range(16):
        angle = 2.0 * math.pi * idx / 16.0
        points.append(
            f"{x + math.cos(angle) * radius:.2f},{y + math.sin(angle) * radius:.2f}"
        )
    shape = " ".join(points)
    return (
        f'    <poly id="{marker_id}" type="{marker_type}" color="{color}"'
        f' fill="true" layer="{layer}" shape="{shape}"/>'
    )


def _write_route_file(execution_route: TruckExecutionRoute, output_path: Path) -> None:
    if not execution_route.sumo_edge_sequence:
        raise ValueError("SUMO 路由 edge 序列为空，无法生成 route 文件")
    route_edges = " ".join(execution_route.sumo_edge_sequence)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '        xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/routes_file.xsd">',
        '    <vType id="truck_phase4" accel="1.0" decel="4.5" sigma="0.0"'
        ' length="42.0" width="12.0" minGap="2.5" maxSpeed="15.0"'
        ' color="0,220,255" guiShape="truck"/>',
        f'    <route id="truck_route" edges="{route_edges}"/>',
        f'    <vehicle id="{execution_route.truck_id}" type="truck_phase4" route="truck_route"'
        ' depart="0" departPos="0" departSpeed="0" color="0,220,255"/>',
        "</routes>",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _write_sumocfg(
    *,
    execution_route: TruckExecutionRoute,
    output_path: Path,
) -> None:
    route_finish_time_sec = max(
        execution_route.total_travel_time_sec,
        execution_route.stops[-1].departure_time_sec if execution_route.stops else 0.0,
    )
    end_time = max(3600, int(route_finish_time_sec + 600))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '               xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/sumoConfiguration.xsd">',
        "    <input>",
        '        <net-file value="truck_route.net.xml"/>',
        '        <route-files value="truck_route.rou.xml"/>',
        '        <additional-files value="poi.add.xml"/>',
        "    </input>",
        "    <time>",
        '        <begin value="0"/>',
        f'        <end value="{end_time}"/>',
        "    </time>",
        "</configuration>",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _write_gui_settings(output_path: Path) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<viewsettings>",
        '    <scheme name="phase4_debug">',
        '        <vehicles vehicleName_show="1" vehicleName_size="80.00"'
        ' vehicleName_color="0,220,255" vehicleName_bgColor="180,0,0,0"'
        ' vehicleName_constantSize="1" vehicleName_onlySelected="0"/>',
        '        <pois poiTextParam="PARAM_TEXT" poi_minSize="0.00"'
        ' poi_exaggeration="1.00" poi_constantSize="0" poiDetail="16"'
        ' poiName_show="0" poiText_show="1" poiText_size="95.00"'
        ' poiText_color="255,255,255" poiText_bgColor="190,0,0,0"'
        ' poiText_constantSize="1" poiText_onlySelected="0"/>',
        '        <polys poly_minSize="0.00" poly_exaggeration="1.00"'
        ' poly_constantSize="0" polyName_show="0" polyType_show="0"/>',
        "    </scheme>",
        '    <delay value="250"/>',
        "</viewsettings>",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _build_execution_route_payload(execution_route: TruckExecutionRoute) -> dict[str, Any]:
    return {
        "truck_id": execution_route.truck_id,
        "total_distance_m": round(float(execution_route.total_distance_m), 6),
        "total_travel_time_sec": round(float(execution_route.total_travel_time_sec), 6),
        "sumo_edge_sequence": list(execution_route.sumo_edge_sequence),
        "stops": [
            {
                "seq": stop.seq,
                "node_type": stop.node_type,
                "node_id": stop.node_id,
                "order_id": stop.order_id,
                "x": round(float(stop.x), 6),
                "y": round(float(stop.y), 6),
                "z": round(float(stop.z), 6),
                "arrival_time_sec": round(float(stop.arrival_time_sec), 6),
                "departure_time_sec": round(float(stop.departure_time_sec), 6),
                "nearest_osm_node_id": stop.nearest_osm_node_id,
                "snap_distance_m": round(float(stop.snap_distance_m), 6),
            }
            for stop in execution_route.stops
        ],
        "segments": [
            {
                "from_node_id": segment.from_node_id,
                "to_node_id": segment.to_node_id,
                "from_node_type": segment.from_node_type,
                "to_node_type": segment.to_node_type,
                "distance_m": round(float(segment.distance_m), 6),
                "travel_time_sec": round(float(segment.travel_time_sec), 6),
                "osm_node_path": list(segment.osm_node_path),
                "sumo_edge_ids": list(segment.sumo_edge_ids),
            }
            for segment in execution_route.segments
        ],
    }


def _build_phase4_debug_trace(
    *,
    truck_id: str,
    ordered_orders: Sequence[Phase4TruckOrder],
    initial_execution_plan: Sequence[Mapping[str, Any]],
    execution_plan: Sequence[Mapping[str, Any]],
    inserted_fixed_nodes: Sequence[Mapping[str, Any]],
    execution_route: TruckExecutionRoute,
    min_future_fixed_nodes: int,
) -> dict[str, Any]:
    visited_orders = [
        stop for stop in execution_route.stops if stop.node_type == "customer"
    ]
    visited_stations = [
        stop for stop in execution_route.stops if stop.node_type == "station"
    ]
    visited_fixed_nodes = [
        stop for stop in execution_route.stops if stop.node_type in {"station", "depot"}
    ]
    return {
        "truck_id": truck_id,
        "min_future_fixed_nodes": min_future_fixed_nodes,
        "ordered_truck_orders": [
            {
                "seq": idx,
                "order_id": item.order.order_id,
                "deadline": float(item.deadline),
                "fulfillment_mode": item.fulfillment_mode,
                "delivery_x": round(float(item.order.delivery_loc.x), 6),
                "delivery_y": round(float(item.order.delivery_loc.y), 6),
            }
            for idx, item in enumerate(ordered_orders)
        ],
        "initial_execution_plan": _serialize_execution_plan(initial_execution_plan),
        "final_execution_plan": _serialize_execution_plan(execution_plan),
        "inserted_fixed_nodes": [dict(item) for item in inserted_fixed_nodes],
        "visited_order_ids": [stop.node_id for stop in visited_orders],
        "visited_station_ids": [stop.node_id for stop in visited_stations],
        "visited_fixed_node_ids": [stop.node_id for stop in visited_fixed_nodes],
        "execution_stop_trace": [
            {
                "seq": stop.seq,
                "node_type": stop.node_type,
                "node_id": stop.node_id,
                "order_id": stop.order_id,
                "arrival_time_sec": round(float(stop.arrival_time_sec), 6),
                "departure_time_sec": round(float(stop.departure_time_sec), 6),
                "nearest_osm_node_id": stop.nearest_osm_node_id,
                "snap_distance_m": round(float(stop.snap_distance_m), 6),
            }
            for stop in execution_route.stops
        ],
    }


def _serialize_execution_plan(
    execution_plan: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "seq": idx,
            "node_type": str(stop["node_type"]),
            "node_id": str(stop["node_id"]),
            "order_id": stop.get("order_id"),
            "x": round(float(stop["position"].x), 6),
            "y": round(float(stop["position"].y), 6),
        }
        for idx, stop in enumerate(execution_plan)
    ]


def _build_validation_report(
    *,
    scene_ctx: TrainingSceneContext,
    truck_orders: Sequence[Phase4TruckOrder],
    execution_route: TruckExecutionRoute,
    truck_backbone_route: Sequence[str],
    truck_eta_map: Mapping[str, float],
    min_future_fixed_nodes: int,
    export_dir: Path,
) -> dict[str, Any]:
    visited_customer_ids = [
        stop.node_id for stop in execution_route.stops if stop.node_type == "customer"
    ]
    expected_customer_ids = [item.order.order_id for item in truck_orders]
    eta_values = [truck_eta_map[node_id] for node_id in truck_backbone_route]

    return {
        "truck_order_count": len(truck_orders),
        "truck_order_ids": expected_customer_ids,
        "execution_stop_count": len(execution_route.stops),
        "execution_customer_count": len(visited_customer_ids),
        "backbone_node_count": len(truck_backbone_route),
        "future_recovery_node_count": len(truck_backbone_route),
        "all_segments_connected": all(bool(segment.osm_node_path) for segment in execution_route.segments),
        "all_expected_customers_visited": expected_customer_ids == visited_customer_ids,
        "all_expected_fixed_nodes_visited": all(
            stop.node_type in {"station", "depot"}
            for stop in execution_route.stops[1:]
            if stop.node_id in truck_backbone_route
        ),
        "eta_monotonic": all(
            eta_values[idx - 1] < eta_values[idx]
            for idx in range(1, len(eta_values))
        ),
        "max_snap_distance_m": round(
            max(stop.snap_distance_m for stop in execution_route.stops),
            6,
        ),
        "snap_within_warn_threshold": all(
            stop.snap_distance_m <= SNAP_WARN_THRESHOLD_M
            for stop in execution_route.stops
        ),
        "bounds_ok": _route_within_bounds(execution_route, scene_ctx.bounds),
        "min_future_fixed_nodes_ok": sum(
            1 for nid in truck_backbone_route
            if any(s.node_id == nid and s.node_type == "station" for s in execution_route.stops)
        ) >= min_future_fixed_nodes,
        "sumo_edge_sequence_non_empty": bool(execution_route.sumo_edge_sequence),
        "sumo_export_dir": str(export_dir),
    }


def _route_within_bounds(
    execution_route: TruckExecutionRoute,
    bounds: Mapping[str, float],
) -> bool:
    min_lng = float(bounds["min_lng"])
    max_lng = float(bounds["max_lng"])
    min_lat = float(bounds["min_lat"])
    max_lat = float(bounds["max_lat"])

    for stop in execution_route.stops:
        lng, lat = stop.position.to_wgs84()
        if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
            return False
    return True


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _scene_bounds_tuple(bounds: Mapping[str, float]) -> tuple[float, float, float, float]:
    return (
        float(bounds["min_lng"]),
        float(bounds["min_lat"]),
        float(bounds["max_lng"]),
        float(bounds["max_lat"]),
    )


def _manhattan_distance(a: Position3D, b: Position3D) -> float:
    return abs(a.x - b.x) + abs(a.y - b.y)


def _road_distance(
    road_graph: Any,
    road_nodes: Mapping[str, tuple[float, float]],
    pos_a: Position3D,
    pos_b: Position3D,
) -> float:
    """路网最短距离（米）；不可达时 fallback 到曼哈顿距离。"""
    node_a = find_nearest_node(road_graph, road_nodes, pos_a.x, pos_a.y)
    node_b = find_nearest_node(road_graph, road_nodes, pos_b.x, pos_b.y)
    if node_a == node_b:
        return pos_a.distance_2d(pos_b)
    dist = shortest_path_length(road_graph, node_a, node_b)
    if dist is None:
        return _manhattan_distance(pos_a, pos_b)
    return dist


def format_execution_route_id_sequence(execution_route: TruckExecutionRoute) -> str:
    return " -> ".join(stop.node_id for stop in execution_route.stops)


def _print_summary(result: Phase4ExportResult) -> None:
    print(f"truck_execution_route: {format_execution_route_id_sequence(result.execution_route)}")
    print(
        json.dumps(
            {
                "truck_backbone_route": list(result.truck_backbone_route),
                "truck_eta_map": result.truck_eta_map,
                "validation_report": result.validation_report,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 Phase 4 卡车路线与 SUMO 验证产物")
    parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_CONFIG_PATH),
        help="训练配置文件路径",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="导出目录；为空时落到 scene_bundle_dir/sumo/phase4_truck_route",
    )
    parser.add_argument(
        "--min-future-fixed-nodes",
        type=int,
        default=None,
        help="`truck_backbone_route` 至少保留的 future fixed nodes 数量",
    )
    args = parser.parse_args(argv)

    result = export_phase4_truck_route(
        config_path=args.config,
        output_dir=args.output_dir or None,
        min_future_fixed_nodes=args.min_future_fixed_nodes,
    )
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
