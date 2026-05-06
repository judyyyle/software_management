from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.entities.primitives import Position3D

from .visibility_graph import Point2D, load_obstacle_polygons, plan_path


class PathPlanner:
    def __init__(self, buildings_geojson: dict):
        self._buildings_geojson: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = buildings_geojson

    def plan(
        self,
        start: Position3D,
        goal: Position3D,
        altitude: float,
    ) -> list[Position3D]:
        start_xy: Point2D = (float(start.x), float(start.y))
        goal_xy: Point2D = (float(goal.x), float(goal.y))
        obstacles = load_obstacle_polygons(self._buildings_geojson, altitude)
        path_xy = plan_path(start_xy, goal_xy, obstacles)

        if not path_xy:
            path_xy = [start_xy, goal_xy]

        z = float(start.z)
        path_3d = [Position3D(x=x, y=y, z=z) for x, y in path_xy]
        if path_3d:
            path_3d[0] = start
            path_3d[-1] = goal
        return path_3d
