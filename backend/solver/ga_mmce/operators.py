from __future__ import annotations

import copy
import random

from .chromosome import Individual, Rendezvous


def _is_depot_node_id(node_id: str) -> bool:
    normalized = str(node_id).strip().upper()
    return normalized == "DEPOT" or normalized.startswith("DEPOT") or normalized.startswith("DEP-")


def find_depot_node(support_node_ids: list[str]) -> str:
    if "DEPOT" in support_node_ids:
        return "DEPOT"

    for node in support_node_ids:
        if _is_depot_node_id(str(node)):
            return node

    raise ValueError("support_node_ids must include a depot node")


def _depot_nodes(support_node_ids: list[str]) -> list[str]:
    return [
        node
        for node in support_node_ids
        if _is_depot_node_id(str(node))
    ]


def make_random_rendezvous_for_gene(
    gene: str,
    support_node_ids: list[str],
    allow_c_recover_station: bool = True,
) -> Rendezvous:
    if gene == "A":
        return None

    if not support_node_ids:
        raise ValueError("support_node_ids must not be empty")

    if gene.startswith("B_"):
        return {
            "launch": random.choice(support_node_ids),
            "recover": random.choice(support_node_ids),
        }

    if gene.startswith("C_"):
        depot_node = find_depot_node(support_node_ids)
        recover_pool = support_node_ids if allow_c_recover_station else _depot_nodes(support_node_ids)
        if not recover_pool:
            raise ValueError("support_node_ids must include a depot recovery node")
        return {
            "launch": depot_node,
            "recover": random.choice(recover_pool),
        }

    raise ValueError(f"unknown assignment gene: {gene}")


def _genes_by_order(ind: Individual) -> dict[str, tuple[str, Rendezvous]]:
    ind.validate()
    return {
        order_id: (gene, copy.deepcopy(rv))
        for order_id, gene, rv in zip(ind.sequence, ind.assignment, ind.rendezvous)
    }


def _build_child(base: Individual, donor: Individual, start: int, end: int) -> Individual:
    n = len(base.sequence)
    child_seq: list[str | None] = [None] * n
    child_seq[start : end + 1] = base.sequence[start : end + 1]

    donor_fill = [order_id for order_id in donor.sequence if order_id not in child_seq]
    fill_idx = 0
    for i in range(n):
        if child_seq[i] is None:
            child_seq[i] = donor_fill[fill_idx]
            fill_idx += 1

    if any(order_id is None for order_id in child_seq):
        raise ValueError("order crossover produced incomplete child sequence")

    child_sequence = [str(order_id) for order_id in child_seq]
    base_map = _genes_by_order(base)
    donor_map = _genes_by_order(donor)

    child_assignment: list[str] = []
    child_rendezvous: list[Rendezvous] = []
    for order_id in child_sequence:
        if order_id not in base_map or order_id not in donor_map:
            raise ValueError(f"missing gene data for order_id={order_id!r}")

        source = base_map if random.random() < 0.5 else donor_map
        gene, rv = source[order_id]
        child_assignment.append(gene)
        child_rendezvous.append(copy.deepcopy(rv))

    child = Individual(
        sequence=child_sequence,
        assignment=child_assignment,
        rendezvous=child_rendezvous,
    )
    child.validate()
    return child


def order_crossover(p1: Individual, p2: Individual) -> tuple[Individual, Individual]:
    p1.validate()
    p2.validate()

    if len(p1.sequence) != len(p2.sequence):
        raise ValueError("parent sequences must have the same length")
    if set(p1.sequence) != set(p2.sequence):
        raise ValueError("parent sequences must contain the same order ids")

    n = len(p1.sequence)
    if n <= 1:
        return copy.deepcopy(p1), copy.deepcopy(p2)

    start, end = sorted(random.sample(range(n), 2))
    return (
        _build_child(p1, p2, start, end),
        _build_child(p2, p1, start, end),
    )


def mutate(
    ind: Individual,
    gene_pool: list[str],
    support_node_ids: list[str],
    p_seq: float,
    p_assign: float,
    p_rendezvous: float,
    allow_c_recover_station: bool = True,
) -> None:
    ind.validate()
    if not gene_pool:
        raise ValueError("gene_pool must not be empty")
    if "A" not in gene_pool:
        raise ValueError('gene_pool must include "A"')
    if not support_node_ids:
        raise ValueError("support_node_ids must not be empty")

    n = len(ind.sequence)

    if n >= 2 and random.random() < p_seq:
        i, j = random.sample(range(n), 2)
        ind.sequence[i], ind.sequence[j] = ind.sequence[j], ind.sequence[i]
        ind.assignment[i], ind.assignment[j] = ind.assignment[j], ind.assignment[i]
        ind.rendezvous[i], ind.rendezvous[j] = ind.rendezvous[j], ind.rendezvous[i]

    for i in range(n):
        if random.random() < p_assign:
            new_gene = random.choice(gene_pool)
            ind.assignment[i] = new_gene
            ind.rendezvous[i] = make_random_rendezvous_for_gene(
                new_gene,
                support_node_ids,
                allow_c_recover_station,
            )

    for i in range(n):
        if random.random() < p_rendezvous:
            ind.rendezvous[i] = make_random_rendezvous_for_gene(
                ind.assignment[i],
                support_node_ids,
                allow_c_recover_station,
            )

    ind.validate()


def tournament_select(population: list[Individual], k: int) -> Individual:
    if not population:
        raise ValueError("population must not be empty")

    candidates = random.sample(population, min(k, len(population)))
    best = min(candidates, key=lambda ind: ind.fitness)
    return copy.deepcopy(best)
