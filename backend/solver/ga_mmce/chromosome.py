from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable


Rendezvous = dict[str, str] | None

_TRUCK_HOME_TYPES = {"TRUCK"}
_DEPOT_HOME_TYPES = {"DEPOT"}


@dataclass
class Individual:
    sequence: list[str]
    assignment: list[str]
    rendezvous: list[Rendezvous]
    fitness: float = float("inf")
    decoded_plan: Any | None = None
    penalties: dict[str, float] = field(default_factory=dict)

    def validate(self) -> None:
        n = len(self.sequence)
        if len(self.assignment) != n:
            raise ValueError("sequence and assignment length mismatch")
        if len(self.rendezvous) != n:
            raise ValueError("sequence and rendezvous length mismatch")

        if len(set(self.sequence)) != n:
            raise ValueError("sequence contains duplicated order ids")

        for gene, rv in zip(self.assignment, self.rendezvous):
            if gene == "A":
                if rv is not None:
                    raise ValueError("A mode must use rendezvous=None")
            elif gene.startswith("B_"):
                if not isinstance(rv, dict):
                    raise ValueError("B mode must use rendezvous dict")
                if not rv.get("launch") or not rv.get("recover"):
                    raise ValueError("B mode rendezvous must include launch and recover")
            elif gene.startswith("C_"):
                if not isinstance(rv, dict):
                    raise ValueError("C mode must use rendezvous dict")
                launch = str(rv.get("launch", ""))
                if launch != "DEPOT" and not launch.upper().startswith("DEPOT"):
                    raise ValueError("C mode launch must be DEPOT/depot id")
                if not rv.get("recover"):
                    raise ValueError("C mode rendezvous must include recover")
            else:
                raise ValueError(f"unknown assignment gene: {gene}")

    def validate_with_context(
        self,
        truck_drone_ids: Iterable[str | int],
        depot_drone_ids: Iterable[str | int],
        valid_drone_ids: Iterable[str | int] | None = None,
    ) -> None:
        self.validate()

        truck_set = _normalize_id_set(truck_drone_ids)
        depot_set = _normalize_id_set(depot_drone_ids)
        valid_set = (
            _normalize_id_set(valid_drone_ids)
            if valid_drone_ids is not None
            else truck_set | depot_set
        )

        for gene in self.assignment:
            if gene == "A":
                continue

            mode, drone_id = _split_assignment_gene(gene)
            if drone_id not in valid_set:
                raise ValueError(f"{gene} uses unknown drone id: {drone_id}")
            if mode == "B" and drone_id not in truck_set:
                raise ValueError(f"B mode drone must be docked on truck: {drone_id}")
            if mode == "C" and drone_id not in depot_set:
                raise ValueError(f"C mode drone must be ready at depot: {drone_id}")


def _coerce_home_type(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        value = value.value
    return str(value).strip().upper()


def _read_field(record: Any, field_name: str) -> Any:
    if isinstance(record, dict):
        return record.get(field_name)
    return getattr(record, field_name, None)


def _normalize_id(value: str | int) -> str:
    return str(value).strip()


def _normalize_id_set(values: Iterable[str | int]) -> set[str]:
    return {_normalize_id(value) for value in values}


def _split_assignment_gene(gene: str) -> tuple[str, str]:
    mode, sep, drone_id = gene.partition("_")
    if sep != "_" or mode not in {"B", "C"} or not drone_id:
        raise ValueError(f"unknown assignment gene: {gene}")
    return mode, drone_id


def extract_drone_numeric_id(drone_id: str | int) -> int:
    if isinstance(drone_id, int):
        if drone_id < 1:
            raise ValueError(f"drone numeric id must be >= 1, got {drone_id}")
        return drone_id

    match = re.search(r"(\d+)$", str(drone_id).strip())
    if match is None:
        raise ValueError(f"drone_id must end with digits: {drone_id!r}")
    return int(match.group(1))


def make_gene_pool(drone_ids: list[str | int]) -> list[str]:
    genes = ["A"]
    for uid in drone_ids:
        drone_id = _normalize_id(uid)
        genes.append(f"B_{drone_id}")
        genes.append(f"C_{drone_id}")
    return genes


def make_gene_pool_by_location(
    truck_drone_ids: Iterable[str | int],
    depot_drone_ids: Iterable[str | int],
) -> list[str]:
    truck_ids = _dedupe_preserve_order(truck_drone_ids)
    depot_ids = _dedupe_preserve_order(depot_drone_ids)
    overlap = set(truck_ids) & set(depot_ids)
    if overlap:
        raise ValueError(f"drone cannot be both truck-docked and depot-ready: {sorted(overlap)}")

    return ["A"] + [f"B_{uid}" for uid in truck_ids] + [f"C_{uid}" for uid in depot_ids]


def make_node_pool(depot_ids: list[str], station_ids: list[str]) -> list[str]:
    return list(depot_ids) + list(station_ids)


def normalize_depot_id(depot_ids: list[str]) -> str:
    if depot_ids:
        return depot_ids[0]
    return "DEPOT"


def make_location_gene(record: Any) -> str:
    drone_id = _read_field(record, "drone_id")
    if drone_id is None:
        raise ValueError("drone record is missing drone_id")

    uid = _normalize_id(drone_id)
    transport_truck_id = _read_field(record, "transport_truck_id")
    home_type = _coerce_home_type(_read_field(record, "home_type"))

    if transport_truck_id:
        return f"B_{uid}"
    if home_type in _TRUCK_HOME_TYPES:
        return f"B_{uid}"
    if home_type in _DEPOT_HOME_TYPES:
        return f"C_{uid}"

    raise ValueError(
        f"cannot infer drone home for {drone_id!r}: "
        f"home_type={home_type!r}, transport_truck_id={transport_truck_id!r}"
    )


def make_gene_pool_from_drones(drone_records: Iterable[Any]) -> list[str]:
    genes = ["A"]
    resolved = sorted({make_location_gene(record) for record in drone_records}, key=_gene_sort_key)
    genes.extend(resolved)
    return genes


def _dedupe_preserve_order(values: Iterable[str | int]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = _normalize_id(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _gene_sort_key(gene: str) -> tuple[int, int]:
    if gene == "A":
        return (0, 0)

    prefix, _, raw_uid = gene.partition("_")
    match = re.search(r"(\d+)$", raw_uid)
    uid = int(match.group(1)) if match else 0
    kind_rank = 1 if prefix == "B" else 2 if prefix == "C" else 3
    return (uid, kind_rank)
