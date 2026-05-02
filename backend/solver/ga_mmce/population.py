from __future__ import annotations

import copy
import random

from .chromosome import Individual
from .operators import make_random_rendezvous_for_gene


def _check_inputs(
    order_ids: list[str],
    gene_pool: list[str],
    support_node_ids: list[str],
) -> None:
    if len(set(order_ids)) != len(order_ids):
        raise ValueError("order_ids contains duplicated order ids")
    if not gene_pool:
        raise ValueError("gene_pool must not be empty")
    if "A" not in gene_pool:
        raise ValueError('gene_pool must include "A"')
    if not support_node_ids:
        raise ValueError("support_node_ids must not be empty")


def make_random_individual(
    order_ids: list[str],
    gene_pool: list[str],
    support_node_ids: list[str],
    allow_c_recover_station: bool = True,
) -> Individual:
    _check_inputs(order_ids, gene_pool, support_node_ids)

    seq = list(order_ids)
    random.shuffle(seq)
    assignment: list[str] = []
    rendezvous = []

    for _ in seq:
        gene = random.choice(gene_pool)
        assignment.append(gene)
        rendezvous.append(
            make_random_rendezvous_for_gene(
                gene,
                support_node_ids,
                allow_c_recover_station,
            )
        )

    ind = Individual(seq, assignment, rendezvous)
    ind.validate()
    return ind


def make_truck_only_individual(order_ids: list[str]) -> Individual:
    ind = Individual(
        sequence=list(order_ids),
        assignment=["A"] * len(order_ids),
        rendezvous=[None] * len(order_ids),
    )
    ind.validate()
    return ind


def make_obl_individual(
    base: Individual,
    gene_pool: list[str],
    support_node_ids: list[str],
    allow_c_recover_station: bool = True,
) -> Individual:
    base.validate()
    _check_inputs(base.sequence, gene_pool, support_node_ids)

    seq = list(reversed(base.sequence))
    assignment: list[str] = []
    rendezvous = []

    for gene in reversed(base.assignment):
        candidates = [candidate for candidate in gene_pool if candidate != gene]
        new_gene = random.choice(candidates or gene_pool)

        assignment.append(new_gene)
        rendezvous.append(
            make_random_rendezvous_for_gene(
                new_gene,
                support_node_ids,
                allow_c_recover_station,
            )
        )

    ind = Individual(seq, assignment, rendezvous)
    ind.validate()
    return ind


def _copy_seed_if_valid(
    seed: Individual,
    order_set: set[str],
    gene_set: set[str],
) -> Individual | None:
    copied = copy.deepcopy(seed)
    try:
        copied.validate()
    except ValueError:
        return None

    if set(copied.sequence) != order_set:
        return None
    if any(gene not in gene_set for gene in copied.assignment):
        return None
    return copied


def initialize_population(
    order_ids: list[str],
    gene_pool: list[str],
    support_node_ids: list[str],
    pop_size: int,
    greedy_seed: Individual | None = None,
    warm_start: list[Individual] | None = None,
    use_truck_only_seed: bool = True,
    use_obl_seed: bool = True,
    allow_c_recover_station: bool = True,
) -> list[Individual]:
    _check_inputs(order_ids, gene_pool, support_node_ids)
    if pop_size <= 0:
        return []

    order_set = set(order_ids)
    gene_set = set(gene_pool)
    population: list[Individual] = []

    if warm_start:
        for seed in warm_start:
            copied = _copy_seed_if_valid(seed, order_set, gene_set)
            if copied is not None:
                population.append(copied)

    if greedy_seed is not None:
        copied = _copy_seed_if_valid(greedy_seed, order_set, gene_set)
        if copied is not None:
            population.append(copied)

    if use_truck_only_seed:
        population.append(make_truck_only_individual(order_ids))

    if use_obl_seed and population:
        population.append(
            make_obl_individual(
                population[0],
                gene_pool,
                support_node_ids,
                allow_c_recover_station,
            )
        )

    while len(population) < pop_size:
        population.append(
            make_random_individual(
                order_ids,
                gene_pool,
                support_node_ids,
                allow_c_recover_station,
            )
        )

    return population[:pop_size]
