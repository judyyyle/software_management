#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — Phase 4 卡车路线导出与 SUMO 验证产物生成。

设计原则：
  - 区分卡车完整执行路线与 `truck_backbone_route`；
  - 完整执行路线包含 `depot / customer / station`；
  - `truck_backbone_route` 仅保留未来固定节点（`station / depot`）；
  - 订单访问顺序：deadline 为主，UTM 曼哈顿距离为 tie-break；
  - ETA 使用 OSM 实际可达路径长度 / `truck.speed` 计算，不使用曼哈顿距离。
"""

from __future__ import annotations

import argparse
import json
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
from environment.geo.exporters.sumo_net_osm import (
    build_sumo_net_artifacts,
    write_sumo_net_artifacts,
)
from environment.geo.osm_service import build_road_graph, find_nearest_node, shortest_path
from training.scene_loader import DEFAULT_CONFIG_PATH, TrainingSceneContext, load_default_scene


CUSTOMER_SERVICE_TIME_SEC = 0.0
DEFAULT_OUTPUT_SUBDIR = Path("sumo") / "phase4_truck_route"
SNAP_WARN_THRESHOLD_M = 60.0


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
    min_future_fixed_nodes: int = 2,
) -> Phase4ExportResult:
    scene_ctx = load_default_scene(config_path=config_path)
    export_dir = _resolve_output_dir(scene_ctx, output_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    depot_id, depot = _require_singleton(scene_ctx.depots, "depot")
    truck_id, truck = _require_singleton(scene_ctx.trucks, "truck")

    truck_orders = _select_truck_orders(scene_ctx)
    ordered_orders = _order_truck_orders(truck_orders, depot.location)
    execution_plan = _build_initial_execution_plan(depot_id, depot.location, ordered_orders)
    execution_plan = _ensure_min_future_fixed_nodes(
        execution_plan,
        stations=scene_ctx.stations,
        min_future_fixed_nodes=min_future_fixed_nodes,
    )

    osm_xml_path = scene_ctx.road_network.xml_path
    if not osm_xml_path:
        raise ValueError("scene_loader 未提供 osm_network.xml 路径，无法执行 Phase 4")
    osm_xml = Path(osm_xml_path).read_text(encoding="utf-8")
    road_graph, road_nodes = build_road_graph(osm_xml, respect_osm_oneway=True)
    net_artifacts = build_sumo_net_artifacts(
        osm_xml,
        _scene_bounds_tuple(scene_ctx.bounds),
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
        road_nodes=road_nodes,
        net_artifacts=net_artifacts,
        output_path=export_dir / "poi.add.xml",
    )
    _write_route_file(execution_route, export_dir / "truck_route.rou.xml")
    _write_sumocfg(
        execution_route=execution_route,
        output_path=export_dir / "truck_route.sumocfg",
    )
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

    validation_report = _build_validation_report(
        scene_ctx=scene_ctx,
        truck_orders=ordered_orders,
        execution_route=execution_route,
        truck_backbone_route=truck_backbone_route,
        truck_eta_map=truck_eta_map,
        min_future_fixed_nodes=min_future_fixed_nodes,
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
        fulfillment_mode = str(entry.get("fulfillment_mode", "")).upper()
        if "TRUCK" not in fulfillment_mode:
            continue
        order_id = str(entry["order_id"])
        order = order_by_id.get(order_id)
        if order is None:
            raise ValueError(f"scene_loader 未找到静态订单对象: {order_id}")
        truck_orders.append(
            Phase4TruckOrder(
                order=order,
                raw=dict(entry),
                deadline=float(order.deadline),
                fulfillment_mode=fulfillment_mode,
            )
        )

    if not truck_orders:
        raise ValueError("未找到任何卡车相关 static_orders，无法生成 Phase 4 路线")
    return tuple(truck_orders)


def _order_truck_orders(
    truck_orders: Sequence[Phase4TruckOrder],
    depot_pos: Position3D,
) -> tuple[Phase4TruckOrder, ...]:
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
                    _manhattan_distance(current_pos, item.order.delivery_loc),
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
) -> list[dict[str, Any]]:
    if min_future_fixed_nodes < 1:
        raise ValueError("min_future_fixed_nodes 必须 >= 1")

    plan = list(execution_plan)
    used_station_ids = {stop["node_id"] for stop in plan if stop["node_type"] == "station"}
    while _count_future_fixed_nodes(plan) < min_future_fixed_nodes:
        best_station = None
        best_index = None
        best_cost = None
        for station_id, station in stations.items():
            if station_id in used_station_ids:
                continue
            station_pos = station.location
            for insert_at in range(1, len(plan)):
                prev_stop = plan[insert_at - 1]
                next_stop = plan[insert_at]
                detour_cost = (
                    _manhattan_distance(prev_stop["position"], station_pos)
                    + _manhattan_distance(station_pos, next_stop["position"])
                    - _manhattan_distance(prev_stop["position"], next_stop["position"])
                )
                candidate = (detour_cost, station_id, insert_at)
                if best_cost is None or candidate < best_cost:
                    best_cost = candidate
                    best_station = station
                    best_index = insert_at

        if best_station is None or best_index is None:
            raise ValueError("没有可插入的 station，无法满足最少 recovery 节点约束")

        plan.insert(
            best_index,
            _make_plan_stop("station", best_station.station_id, best_station.location),
        )
        used_station_ids.add(best_station.station_id)
    return plan


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
    return sum(1 for stop in execution_plan[1:] if stop["node_type"] in {"station", "depot"})


def _materialize_execution_route(
    *,
    truck_id: str,
    truck_speed: float,
    execution_plan: Sequence[Mapping[str, Any]],
    road_graph: Any,
    road_nodes: Mapping[str, tuple[float, float]],
    directed_step_to_edge: Mapping[tuple[str, str], str],
) -> TruckExecutionRoute:
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
        departure_time = arrival_time + (CUSTOMER_SERVICE_TIME_SEC if cur_stop.node_type == "customer" else 0.0)

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
            ' color="255,96,0" fill="false" layer="8" lineWidth="6"'
            f' shape="{shape}"/>'
        )

    for depot_id, depot in scene_ctx.depots.items():
        x, y = local_xy(depot.location)
        lines.append(
            _format_poi_xml(
                poi_id=depot_id,
                x=x,
                y=y,
                color="0,128,255",
                poi_type="depot",
            )
        )
    for station_id, station in scene_ctx.stations.items():
        x, y = local_xy(station.location)
        lines.append(
            _format_poi_xml(
                poi_id=station_id,
                x=x,
                y=y,
                color="0,180,0",
                poi_type="station",
            )
        )
    for stop in execution_route.stops:
        if stop.node_type != "customer":
            continue
        x, y = local_xy(stop.position)
        lines.append(
            _format_poi_xml(
                poi_id=stop.node_id,
                x=x,
                y=y,
                color="255,64,64",
                poi_type="order",
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
) -> str:
    return (
        f'    <poi id="{poi_id}" type="{poi_type}" color="{color}" '
        f'x="{x:.2f}" y="{y:.2f}" layer="10"/>'
    )


def _write_route_file(execution_route: TruckExecutionRoute, output_path: Path) -> None:
    if not execution_route.sumo_edge_sequence:
        raise ValueError("SUMO 路由 edge 序列为空，无法生成 route 文件")
    route_edges = " ".join(execution_route.sumo_edge_sequence)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '        xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/routes_file.xsd">',
        '    <vType id="truck_phase4" accel="1.0" decel="4.5" sigma="0.0" length="8.0" maxSpeed="15.0"/>',
        f'    <route id="truck_route" edges="{route_edges}"/>',
        f'    <vehicle id="{execution_route.truck_id}" type="truck_phase4" route="truck_route" depart="0"/>',
        "</routes>",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _write_sumocfg(
    *,
    execution_route: TruckExecutionRoute,
    output_path: Path,
) -> None:
    end_time = max(3600, int(execution_route.total_travel_time_sec + 600))
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
        "min_future_fixed_nodes_ok": len(truck_backbone_route) >= min_future_fixed_nodes,
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


def _print_summary(result: Phase4ExportResult) -> None:
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
        default=2,
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
