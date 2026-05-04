from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GAConfig:
    population_size: int = 80
    generations: int = 120
    min_generations: int = 80
    early_stopping_patience: int = 40
    improvement_tolerance: float = 1e-3
    elite_ratio: float = 0.08
    tournament_k: int = 3

    crossover_rate: float = 0.9
    mutation_rate_sequence: float = 0.2
    mutation_rate_assignment: float = 0.15
    mutation_rate_rendezvous: float = 0.20

    use_greedy_seed: bool = True
    use_truck_only_seed: bool = True
    use_obl_seed: bool = True
    use_balanced_initialization: bool = True
    use_b_seeded_initialization: bool = True

    mutation_mode_prob_a: float = 0.40
    mutation_mode_prob_b: float = 0.25
    mutation_mode_prob_c: float = 0.35

    enable_repair: bool = True
    repair_penalty_factor: float = 1000.0

    big_m: float = 1e9

    weight_completion: float = 1.0
    weight_delay: float = 10.0
    weight_energy: float = 0.1
    weight_waiting: float = 5.0
    weight_infeasible: float = 100000.0
    weight_truck_distance: float = 1.0
    weight_uav_distance: float = 1.0

    max_runtime_seconds: float | None = None
    random_seed: int | None = 42
    verbose: bool = False
    log_interval: int = 10
    diagnostics_enabled: bool = True
    diagnostics_dir: str = "logs"
    save_evolution_csv: bool = True
    save_evolution_plots: bool = True
    b_candidate_precheck: bool = True

    # 是否允许 C 模式无人机送完后落在充换电站；若 False，则 C 必须回仓。
    allow_depot_drone_recover_at_station: bool = True

    # 若卡车与无人机回收同步失败，是否作为软惩罚而不是直接判死。
    soft_rendezvous_violation: bool = True

    truck_wait_max_s: float = 10.0
