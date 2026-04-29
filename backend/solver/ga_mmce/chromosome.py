from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable


_TRUCK_HOME_TYPES = {"TRUCK"}
_DEPOT_HOME_TYPES = {"DEPOT"}


@dataclass
class Individual:
    sequence: list[str]
    assignment: list[str]
    fitness: float = float("inf")
    decoded_plan: Any | None = None
    penalties: dict[str, float] = field(default_factory=dict)

    def validate(self) -> None:
        if len(self.sequence) != len(self.assignment):
            raise ValueError(
                f"sequence length {len(self.sequence)} != "
                f"assignment length {len(self.assignment)}"
            )

        if len(set(self.sequence)) != len(self.sequence):
            raise ValueError("sequence contains duplicated order ids")


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
    for uid in sorted({extract_drone_numeric_id(drone_id) for drone_id in drone_ids}):
        genes.append(f"B_{uid}")
        genes.append(f"C_{uid}")
    return genes


def make_location_gene(record: Any) -> str:
    drone_id = _read_field(record, "drone_id")
    if drone_id is None:
        raise ValueError("drone record is missing drone_id")

    uid = extract_drone_numeric_id(drone_id)
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


def _gene_sort_key(gene: str) -> tuple[int, int]:
    if gene == "A":
        return (0, 0)

    prefix, _, raw_uid = gene.partition("_")
    uid = int(raw_uid) if raw_uid.isdigit() else 0
    kind_rank = 1 if prefix == "B" else 2 if prefix == "C" else 3
    return (uid, kind_rank)
