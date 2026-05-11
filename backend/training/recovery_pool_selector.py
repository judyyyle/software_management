#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recovery pool deterministic selector shared by env_adapter and planner_bridge.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def select_recovery_pool_for_order(
    *,
    order: Any,
    truck_backbone_route: Sequence[str],
    truck_eta_map: Mapping[str, float],
    node_states: Mapping[str, Any],
    max_candidates: int,
    future_scan_limit: int,
    drone_cruise_speed: float,
    upper_horizon_sec: float,
) -> tuple[str, ...]:
    """Expose fixed nodes that the truck will still visit.

    Per-drone timing, energy, and top-K truncation happen in CandidateBuilder.
    The coarse layer only keeps the route boundary.
    """
    if not truck_backbone_route:
        return ()

    selected: list[str] = []
    seen: set[str] = set()
    for node_id in truck_backbone_route:
        node_id = str(node_id)
        if node_id in seen:
            continue
        if node_id not in truck_eta_map or node_id not in node_states:
            continue
        seen.add(node_id)
        selected.append(node_id)

    return tuple(selected)
