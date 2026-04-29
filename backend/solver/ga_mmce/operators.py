from __future__ import annotations

import copy
import random

from .chromosome import Individual


def _assignment_by_order(ind: Individual) -> dict[str, str]:
    ind.validate()
    return dict(zip(ind.sequence, ind.assignment))


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

    base_gene = _assignment_by_order(base)
    donor_gene = _assignment_by_order(donor)

    child_assignment: list[str] = []
    for order_id in child_sequence:
        if order_id not in base_gene or order_id not in donor_gene:
            raise ValueError(f"missing assignment gene for order_id={order_id!r}")
        if random.random() < 0.5:
            child_assignment.append(base_gene[order_id])
        else:
            child_assignment.append(donor_gene[order_id])

    child = Individual(sequence=child_sequence, assignment=child_assignment)
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


def mutate(ind: Individual, gene_pool: list[str], p_seq: float, p_assign: float) -> None:
    ind.validate()
    n = len(ind.sequence)

    if n >= 2 and random.random() < p_seq:
        i, j = random.sample(range(n), 2)
        ind.sequence[i], ind.sequence[j] = ind.sequence[j], ind.sequence[i]
        ind.assignment[i], ind.assignment[j] = ind.assignment[j], ind.assignment[i]

    if gene_pool:
        for i in range(n):
            if random.random() < p_assign:
                ind.assignment[i] = random.choice(gene_pool)

    ind.validate()


def tournament_select(population: list[Individual], k: int) -> Individual:
    if not population:
        raise ValueError("population must not be empty")

    candidates = random.sample(population, min(max(1, k), len(population)))
    best = min(candidates, key=lambda ind: ind.fitness)
    return copy.deepcopy(best)
