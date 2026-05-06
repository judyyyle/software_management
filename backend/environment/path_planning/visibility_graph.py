from __future__ import annotations

import heapq
import math
from collections.abc import Mapping, Sequence
from typing import Any

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape

Point2D = tuple[float, float]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iter_features(buildings_geojson: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    if buildings_geojson is None:
        return []
    if isinstance(buildings_geojson, Mapping):
        if buildings_geojson.get("type") == "FeatureCollection":
            features = buildings_geojson.get("features", [])
            if isinstance(features, list):
                return [f for f in features if isinstance(f, Mapping)]
        return []
    if isinstance(buildings_geojson, Sequence):
        return [f for f in buildings_geojson if isinstance(f, Mapping)]
    return []


def _extract_polygons(feature: Mapping[str, Any]) -> list[Polygon]:
    geometry = feature.get("geometry")
    if not isinstance(geometry, Mapping):
        return []

    geom = shape(geometry)
    if geom.is_empty:
        return []

    if isinstance(geom, Polygon):
        candidates = [geom]
    elif isinstance(geom, MultiPolygon):
        candidates = list(geom.geoms)
    else:
        return []

    polygons: list[Polygon] = []
    for poly in candidates:
        if poly.is_empty:
            continue
        normalized = poly if poly.is_valid else poly.buffer(0)
        if isinstance(normalized, Polygon) and not normalized.is_empty:
            polygons.append(normalized)
    return polygons


def load_obstacle_polygons(
    buildings_geojson: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    drone_altitude: float,
) -> list[Polygon]:
    obstacles: list[Polygon] = []
    for feature in _iter_features(buildings_geojson):
        props = feature.get("properties", {})
        if not isinstance(props, Mapping):
            props = {}
        nf = _as_bool(props.get("nf", False))
        height = _as_float(props.get("h", 0.0), default=0.0)
        if not (nf or height > float(drone_altitude)):
            continue
        obstacles.extend(_extract_polygons(feature))
    return obstacles


def _collect_nodes(start: Point2D, goal: Point2D, obstacles: list[Polygon]) -> list[Point2D]:
    nodes: list[Point2D] = [start, goal]
    seen: set[Point2D] = {start, goal}
    for poly in obstacles:
        coords = list(poly.exterior.coords)
        if len(coords) <= 1:
            continue
        for x, y in coords[:-1]:
            node = (float(x), float(y))
            if node in seen:
                continue
            seen.add(node)
            nodes.append(node)
    return nodes


def _is_visible(p1: Point2D, p2: Point2D, obstacles: list[Polygon]) -> bool:
    line = LineString([p1, p2])
    p1_geom = Point(p1)
    p2_geom = Point(p2)
    for poly in obstacles:
        # 可见性核心约束：线段若穿越或落在障碍内部，则不可连边。
        if line.crosses(poly) or line.within(poly):
            return False
        if poly.contains(p1_geom) or poly.contains(p2_geom):
            return False
    return True


def _build_visibility_graph(nodes: list[Point2D], obstacles: list[Polygon]) -> dict[int, list[tuple[int, float]]]:
    graph: dict[int, list[tuple[int, float]]] = {idx: [] for idx in range(len(nodes))}
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if not _is_visible(nodes[i], nodes[j], obstacles):
                continue
            dist = math.dist(nodes[i], nodes[j])
            graph[i].append((j, dist))
            graph[j].append((i, dist))
    return graph


def _astar_shortest_path(
    nodes: list[Point2D],
    graph: dict[int, list[tuple[int, float]]],
    start_idx: int,
    goal_idx: int,
) -> list[Point2D]:
    open_heap: list[tuple[float, int]] = [(0.0, start_idx)]
    came_from: dict[int, int] = {}
    g_cost: dict[int, float] = {start_idx: 0.0}
    closed: set[int] = set()

    while open_heap:
        _, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        if cur == goal_idx:
            break
        closed.add(cur)

        for nxt, edge_cost in graph.get(cur, []):
            tentative = g_cost[cur] + edge_cost
            if tentative >= g_cost.get(nxt, float("inf")):
                continue
            came_from[nxt] = cur
            g_cost[nxt] = tentative
            h = math.dist(nodes[nxt], nodes[goal_idx])
            heapq.heappush(open_heap, (tentative + h, nxt))

    if goal_idx not in g_cost:
        return []

    path_idx: list[int] = [goal_idx]
    cur = goal_idx
    while cur != start_idx:
        cur = came_from[cur]
        path_idx.append(cur)
    path_idx.reverse()
    return [nodes[idx] for idx in path_idx]


def plan_path(start: Point2D, goal: Point2D, obstacles: list[Polygon]) -> list[Point2D]:
    if start == goal:
        return [start]
    if _is_visible(start, goal, obstacles):
        return [start, goal]

    nodes = _collect_nodes(start, goal, obstacles)
    graph = _build_visibility_graph(nodes, obstacles)
    return _astar_shortest_path(nodes, graph, start_idx=0, goal_idx=1)
