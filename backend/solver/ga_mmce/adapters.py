from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable

from .chromosome import Individual, make_gene_pool_by_location, make_node_pool
from .operators import find_depot_node, make_random_rendezvous_for_gene


_DEPOT_LAUNCH_TOLERANCE_M = 30.0


@dataclass
class GAAdapterContext:
    order_ids: list[str]
    truck_drone_ids: list[str]
    depot_drone_ids: list[str]
    all_drone_ids: list[str]
    truck_ids: list[str]
    depot_ids: list[str]
    station_ids: list[str]
    support_node_ids: list[str]
    gene_pool: list[str]


def _read_field(record: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(field_name, default)
    return getattr(record, field_name, default)


def _status_name(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        value = value.value
    return str(value).strip().upper()


def _as_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    return list(value)


def _as_keys_or_ids(value: Any, id_field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [str(key) for key in value.keys()]
    return [str(_read_field(item, id_field, "")) for item in value if _read_field(item, id_field, "")]


def _entity_mgr(state: Any) -> Any:
    return (
        _read_field(state, "entity_mgr")
        or _read_field(state, "entity_manager")
        or state
    )


def _mapping(state: Any, field_name: str) -> Any:
    mgr = _entity_mgr(state)
    return _read_field(mgr, field_name)


def _order_sources(state: Any) -> list[Any]:
    sources: list[Any] = []

    for field_name in ("orders", "pending_orders"):
        value = _read_field(state, field_name)
        if value is not None:
            sources.extend(_as_values(value))

    for manager_name in ("order_mgr", "order_manager"):
        manager = _read_field(state, manager_name)
        if manager is None:
            continue
        for field_name in ("pending_orders", "assigned_orders", "orders"):
            value = _read_field(manager, field_name)
            if value is not None:
                sources.extend(_as_values(value))

    if sources:
        return sources

    # Some tests pass the EntityManager directly and store pending orders at depots.
    for depot in _as_values(_mapping(state, "depots")):
        sources.extend(_as_values(_read_field(depot, "pending_orders")))
    return sources


def _is_locked_order(order: Any) -> bool:
    for field_name in ("locked", "is_locked", "locked_action", "running_action"):
        value = _read_field(order, field_name)
        if bool(value):
            return True
    return False


def _is_active_order(order: Any) -> bool:
    if _is_locked_order(order):
        return False

    status = _status_name(_read_field(order, "status"))
    if status in {"COMPLETED", "REJECTED", "CANCELLED", "CANCELED"}:
        return False
    return bool(_read_field(order, "order_id"))


def extract_active_order_ids(state) -> list[str]:
    """Return order IDs that may still be optimized by GA."""
    seen: set[str] = set()
    order_ids: list[str] = []
    for order in _order_sources(state):
        order_id = str(_read_field(order, "order_id", "")).strip()
        if not order_id or order_id in seen or not _is_active_order(order):
            continue
        seen.add(order_id)
        order_ids.append(order_id)
    return order_ids


def _locked_drone_ids(state: Any) -> set[str]:
    locked: set[str] = set()
    for field_name in ("locked_drone_ids", "locked_drones", "running_drone_ids", "busy_drone_ids"):
        value = _read_field(state, field_name)
        if value is not None:
            locked.update(str(item) for item in value)

    for action in _as_values(_read_field(state, "locked_actions")):
        drone_id = _read_field(action, "drone_id")
        if drone_id:
            locked.add(str(drone_id))
    return locked


def _drone_is_idle(drone: Any) -> bool:
    status = _read_field(drone, "status")
    if hasattr(status, "is_dispatchable"):
        return bool(status.is_dispatchable)
    return _status_name(status) == "IDLE"


def _has_pending_route(drone: Any) -> bool:
    value = _read_field(drone, "has_pending_route")
    if value is not None:
        return bool(value)
    route_plan = _read_field(drone, "route_plan", [])
    waypoint_index = int(_read_field(drone, "current_waypoint_index", 0) or 0)
    return waypoint_index < len(route_plan)


def _has_enough_standby_energy(drone: Any) -> bool:
    battery_current = _read_field(drone, "battery_current")
    if battery_current is None:
        return True
    safe_margin = float(_read_field(drone, "safe_margin_j", 0.0) or 0.0)
    return float(battery_current) > safe_margin


def _drone_is_available(drone: Any, locked_ids: set[str]) -> bool:
    drone_id = str(_read_field(drone, "drone_id", "")).strip()
    if not drone_id or drone_id in locked_ids:
        return False
    if not _drone_is_idle(drone):
        return False
    if not _has_enough_standby_energy(drone):
        return False
    if _read_field(drone, "carrying_order_id"):
        return False
    if _read_field(drone, "waiting_recovery_station_id"):
        return False
    if _has_pending_route(drone):
        return False
    return True


def _truck_docked_drone_ids(state: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for truck in _as_values(_mapping(state, "trucks")):
        for drone_id in _read_field(truck, "docked_drones", []) or []:
            normalized = str(drone_id).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    return result


def extract_truck_drone_ids(state) -> list[str]:
    """Return currently truck-docked drones usable for B-mode genes."""
    drones = _mapping(state, "drones") or {}
    locked_ids = _locked_drone_ids(state)
    result: list[str] = []
    for drone_id in _truck_docked_drone_ids(state):
        drone = drones.get(drone_id) if isinstance(drones, dict) else None
        if drone is None or not _drone_is_available(drone, locked_ids):
            continue
        result.append(drone_id)
    return result


def _distance_2d(pos_a: Any, pos_b: Any) -> float | None:
    if pos_a is None or pos_b is None:
        return None
    if hasattr(pos_a, "distance_2d"):
        return float(pos_a.distance_2d(pos_b))
    if all(hasattr(pos, "x") and hasattr(pos, "y") for pos in (pos_a, pos_b)):
        dx = float(pos_a.x) - float(pos_b.x)
        dy = float(pos_a.y) - float(pos_b.y)
        return (dx * dx + dy * dy) ** 0.5
    return None


def _is_at_depot(drone: Any, depot: Any) -> bool:
    distance = _distance_2d(_read_field(drone, "current_loc"), _read_field(depot, "location"))
    if distance is None:
        return True
    return distance <= _DEPOT_LAUNCH_TOLERANCE_M


def extract_depot_drone_ids(state) -> list[str]:
    """Return currently depot-ready drones usable for C-mode genes."""
    drones = _mapping(state, "drones") or {}
    truck_docked = set(_truck_docked_drone_ids(state))
    locked_ids = _locked_drone_ids(state)
    seen: set[str] = set()
    result: list[str] = []

    for depot in _as_values(_mapping(state, "depots")):
        for drone_id in _read_field(depot, "idle_drones", []) or []:
            normalized = str(drone_id).strip()
            if not normalized or normalized in seen or normalized in truck_docked:
                continue
            drone = drones.get(normalized) if isinstance(drones, dict) else None
            if drone is None or not _drone_is_available(drone, locked_ids):
                continue
            if _read_field(drone, "transport_truck_id"):
                continue
            if not _is_at_depot(drone, depot):
                continue
            seen.add(normalized)
            result.append(normalized)

    return result


def extract_all_drone_ids(state) -> list[str]:
    """Return all drone IDs in the current entity manager."""
    drones = _mapping(state, "drones")
    if isinstance(drones, dict):
        return [str(key) for key in drones.keys()]
    return [str(_read_field(drone, "drone_id", "")) for drone in _as_values(drones) if _read_field(drone, "drone_id", "")]


def extract_truck_ids(state) -> list[str]:
    return _as_keys_or_ids(_mapping(state, "trucks"), "truck_id")


def extract_depot_ids(state) -> list[str]:
    return _as_keys_or_ids(_mapping(state, "depots"), "depot_id")


def extract_station_ids(state) -> list[str]:
    return _as_keys_or_ids(_mapping(state, "stations"), "station_id")


def extract_support_node_ids(state) -> list[str]:
    return make_node_pool(extract_depot_ids(state), extract_station_ids(state))


def build_ga_context(state) -> GAAdapterContext:
    order_ids = extract_active_order_ids(state)
    truck_drone_ids = extract_truck_drone_ids(state)
    depot_drone_ids = extract_depot_drone_ids(state)
    all_drone_ids = extract_all_drone_ids(state)
    truck_ids = extract_truck_ids(state)
    depot_ids = extract_depot_ids(state)
    station_ids = extract_station_ids(state)
    support_node_ids = make_node_pool(depot_ids, station_ids)
    gene_pool = make_gene_pool_by_location(truck_drone_ids, depot_drone_ids)

    return GAAdapterContext(
        order_ids=order_ids,
        truck_drone_ids=truck_drone_ids,
        depot_drone_ids=depot_drone_ids,
        all_drone_ids=all_drone_ids,
        truck_ids=truck_ids,
        depot_ids=depot_ids,
        station_ids=station_ids,
        support_node_ids=support_node_ids,
        gene_pool=gene_pool,
    )


def clone_state_for_decode(state):
    return copy.deepcopy(state)


def _iter_allocations(greedy_plan) -> Iterable[Any]:
    if greedy_plan is None:
        return []
    if isinstance(greedy_plan, dict):
        return greedy_plan.values()
    if isinstance(greedy_plan, list):
        return greedy_plan
    for field_name in ("allocations", "results", "assignments"):
        value = _read_field(greedy_plan, field_name)
        if value is not None:
            return value.values() if isinstance(value, dict) else value
    return []


def _repair_rendezvous_if_needed(
    gene: str,
    rv: dict[str, str] | None,
    support_node_ids: list[str],
    allow_c_recover_station: bool,
) -> dict[str, str] | None:
    if gene == "A":
        return None
    if not isinstance(rv, dict):
        return make_random_rendezvous_for_gene(
            gene,
            support_node_ids,
            allow_c_recover_station,
        )

    support_set = {str(node) for node in support_node_ids}
    if rv.get("launch") not in support_set or rv.get("recover") not in support_set:
        return make_random_rendezvous_for_gene(
            gene,
            support_node_ids,
            allow_c_recover_station,
        )
    return rv


def _gene_context_from_pool(gene_pool: list[str]) -> tuple[list[str], list[str], list[str]]:
    truck_drone_ids: list[str] = []
    depot_drone_ids: list[str] = []
    for gene in gene_pool:
        if gene.startswith("B_"):
            truck_drone_ids.append(gene.split("_", 1)[1])
        elif gene.startswith("C_"):
            depot_drone_ids.append(gene.split("_", 1)[1])

    all_drone_ids = sorted(set(truck_drone_ids) | set(depot_drone_ids))
    return truck_drone_ids, depot_drone_ids, all_drone_ids


def greedy_plan_to_individual(
    greedy_plan,
    order_ids: list[str],
    gene_pool: list[str],
    support_node_ids: list[str],
    allow_c_recover_station: bool = True,
) -> Individual | None:
    if not order_ids:
        return Individual(sequence=[], assignment=[], rendezvous=[])
    if not gene_pool or "A" not in gene_pool:
        raise ValueError('gene_pool must include "A"')
    if not support_node_ids:
        raise ValueError("support_node_ids must not be empty")

    allocations_by_order: dict[str, Any] = {}
    for alloc in _iter_allocations(greedy_plan):
        order_id = str(_read_field(alloc, "order_id", "")).strip()
        if order_id:
            allocations_by_order[order_id] = alloc

    gene_set = set(gene_pool)
    sequence = list(order_ids)
    assignment: list[str] = []
    rendezvous = []

    for order_id in sequence:
        alloc = allocations_by_order.get(order_id)
        mode = _status_name(_read_field(alloc, "mode")) if alloc is not None else ""
        drone_id = str(_read_field(alloc, "drone_id", "") or "").strip() if alloc is not None else ""

        if mode == "A" or not drone_id:
            gene = "A"
            rv = None
        elif mode in {"B", "B_WAIT", "B_DYNAMIC"}:
            gene = f"B_{drone_id}"
            if gene not in gene_set:
                gene = "A"
                rv = None
            else:
                launch = (
                    _read_field(alloc, "launch_station_id")
                    or _read_field(alloc, "launch_node_id")
                    or _read_field(alloc, "recovery_station_id")
                    or support_node_ids[0]
                )
                recover = (
                    _read_field(alloc, "recovery_station_id")
                    or _read_field(alloc, "recover_node_id")
                    or launch
                )
                rv = {"launch": str(launch), "recover": str(recover)}
        elif mode == "C":
            gene = f"C_{drone_id}"
            if gene not in gene_set:
                gene = "A"
                rv = None
            else:
                depot_node = find_depot_node(support_node_ids)
                recover = (
                    _read_field(alloc, "recovery_station_id")
                    or _read_field(alloc, "recover_node_id")
                    or depot_node
                )
                rv = {"launch": str(depot_node), "recover": str(recover)}
        else:
            gene = "A"
            rv = None

        rv = _repair_rendezvous_if_needed(
            gene,
            rv,
            support_node_ids,
            allow_c_recover_station,
        )
        assignment.append(gene)
        rendezvous.append(rv)

    individual = Individual(sequence=sequence, assignment=assignment, rendezvous=rendezvous)
    individual.validate()
    truck_drone_ids, depot_drone_ids, all_drone_ids = _gene_context_from_pool(gene_pool)
    individual.validate_with_context(
        truck_drone_ids,
        depot_drone_ids,
        valid_drone_ids=all_drone_ids,
        support_node_ids=support_node_ids,
    )
    return individual
