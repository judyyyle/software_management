from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GAConfig:
    population_size: int = 80
    generations: int = 120
    elite_ratio: float = 0.08
    tournament_k: int = 3

    crossover_rate: float = 0.9
    mutation_rate_sequence: float = 0.2
    mutation_rate_assignment: float = 0.15

    use_greedy_seed: bool = True
    use_truck_only_seed: bool = True
    use_obl_seed: bool = True

    big_m: float = 1e9

    weight_completion: float = 1.0
    weight_delay: float = 10.0
    weight_energy: float = 0.1
    weight_waiting: float = 5.0
    weight_infeasible: float = 100000.0

    max_runtime_seconds: float | None = None
    random_seed: int | None = None
    verbose: bool = False
