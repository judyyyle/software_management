#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recovery pool deterministic selector shared by env_adapter and planner_bridge.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


_TIME_EPS = 1e-6


def select_recovery_pool_for_order(
    *,
    order: Any,
    truck_backbone_route: Sequence[str],
    truck_eta_map: Mapping[str, float],
    node_states: Mapping[str, Any],
    max_candidates: int,
    future_scan_limit: int,
    drone_cruise_speed: float,
) -> tuple[str, ...]:
    """
    Scan a longer future-backbone prefix and deterministically keep the best top-K.

    The selector intentionally uses only fields already available in the current
    runtime/coarse-plan path:
      - proxy rendezvous margin: truck ETA minus delivery->recovery fly-time lower bound
      - truck ETA
      - node type
      - queue time estimate
      - recovery-node distance to the order delivery location
    """
    if max_candidates <= 0 or future_scan_limit <= 0 or not truck_backbone_route:
        return ()

    scan_count = min(
        len(truck_backbone_route),
        max(max_candidates, future_scan_limit),
    )
    scanned_route = tuple(truck_backbone_route[:scan_count])
    if not scanned_route:
        return ()

    cruise_speed = max(float(drone_cruise_speed), _TIME_EPS)
    scored_nodes: list[tuple[tuple[float, float, int, float, float, str], str]] = []
    scored_node_ids: set[str] = set()

    for node_id in scanned_route:
        truck_eta = truck_eta_map.get(node_id)
        node_state = node_states.get(node_id)
        if truck_eta is None or node_state is None:
            continue

        delivery_to_node_dist = float(
            node_state.position.distance_2d(order.delivery_loc)
        )
        fly_time_lb = delivery_to_node_dist / cruise_speed
        proxy_rendezvous_margin = float(truck_eta) - fly_time_lb
        queue_time_est = _predicted_queue_time_est(node_state)
        score_key = (
            -proxy_rendezvous_margin,
            float(truck_eta),
            _node_type_rank(str(node_state.node_type)),
            queue_time_est,
            delivery_to_node_dist,
            str(node_id),
        )
        scored_nodes.append((score_key, str(node_id)))
        scored_node_ids.add(str(node_id))

    scored_nodes.sort(key=lambda item: item[0])

    selected: list[str] = []
    for _score_key, node_id in scored_nodes:
        if node_id in selected:
            continue
        selected.append(node_id)
        if len(selected) >= max_candidates:
            return tuple(selected)

    for node_id in scanned_route:
        node_id = str(node_id)
        if node_id in scored_node_ids or node_id in selected:
            continue
        selected.append(node_id)
        if len(selected) >= max_candidates:
            break

    return tuple(selected)


def _predicted_queue_time_est(node_state: Any) -> float:
    if int(node_state.available_slots) > 0:
        return 0.0
    return (int(node_state.queue_length) + 1) * float(node_state.swap_time)


def _node_type_rank(node_type: str) -> int:
    if node_type == "station":
        return 0
    if node_type == "depot":
        return 1
    return 2
