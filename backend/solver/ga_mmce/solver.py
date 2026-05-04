from __future__ import annotations

import copy
import logging
import math
import random
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import build_ga_context, greedy_plan_to_individual
from .chromosome import Individual
from .config import GAConfig
from .diagnostics import write_evolution_csv, write_evolution_plots
from .decoder import DispatchPlan, GADecoder
from .fitness import compute_fitness
from .operators import mutate, order_crossover, tournament_select
from .physical_evaluator import PhysicalEvaluator
from .population import initialize_population


logger = logging.getLogger(__name__)
DEBUG_LOG_PATH = Path(__file__).resolve().parents[1] / "ga_mmce_debug_log"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class GAStateView:
    entity_mgr: Any
    orders: dict[str, Any]
    current_time: float
    bbox: dict | None = None
    scene_id: str | None = None


class GAMMCESolver:
    """Main GA-MMCE solver orchestrating population evolution and decoding."""

    def __init__(self, entity_mgr: Any, config: GAConfig | None = None):
        self.entity_mgr = entity_mgr
        self.config = config or GAConfig()
        self.greedy_helper = self._make_greedy_helper(entity_mgr)
        self.evaluator = PhysicalEvaluator(entity_mgr, self.greedy_helper, self.config)
        self.decoder = GADecoder(self.config, self.evaluator)
        self.last_best_individual: Individual | None = None
        self.last_best_decode_result: Any | None = None
        self._debug_eval_errors: dict[str, int] = {}
        self._evolution_rows: list[dict[str, Any]] = []
        self._mutation_stats: dict[str, Any] = self._empty_mutation_stats()
        self._last_generation_seconds: float = 0.0
        self._early_stop_info: dict[str, Any] = {}
        self._b_precheck_by_order: dict[str, dict[str, Any]] = {}

        if self.config.random_seed is not None:
            random.seed(self.config.random_seed)

    def dispatch(
        self,
        pending_orders: dict[str, Any],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        state = GAStateView(
            entity_mgr=self.entity_mgr,
            orders=dict(pending_orders),
            current_time=current_time,
            bbox=bbox,
            scene_id=scene_id,
        )
        return self.solve(state, dispatch_type="full")

    def dispatch_incremental(
        self,
        new_orders: dict[str, Any],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        state = GAStateView(
            entity_mgr=self.entity_mgr,
            orders=dict(new_orders),
            current_time=current_time,
            bbox=bbox,
            scene_id=scene_id,
        )
        return self.solve(state, dispatch_type="incremental")

    def dispatch_replan_current_state(
        self,
        replan_orders: dict[str, Any],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        state = GAStateView(
            entity_mgr=self.entity_mgr,
            orders=dict(replan_orders),
            current_time=current_time,
            bbox=bbox,
            scene_id=scene_id,
        )
        return self.solve(state, dispatch_type="dynamic_replan")

    def should_replan_unfinished(self) -> bool:
        return True

    def get_active_contracts(self) -> list[Any]:
        return []

    def fulfill_contract(self, contract_id: str) -> None:
        return None

    def build_incremental_route_from_stops(
        self,
        truck: Any,
        ordered_stops: list[dict],
        current_time: float,
    ) -> Any:
        return self.greedy_helper.build_incremental_route_from_stops(
            truck=truck,
            ordered_stops=ordered_stops,
            current_time=current_time,
        )

    def solve(
        self,
        state: Any,
        warm_start: Individual | list[Individual] | None = None,
        dispatch_type: str = "ga_mmce",
    ) -> DispatchPlan:
        started = time.time()
        self._debug_eval_errors = {}
        self._evolution_rows = []
        self._mutation_stats = self._empty_mutation_stats()
        self._last_generation_seconds = 0.0
        self._early_stop_info = {
            "last_improvement_gen": -1,
            "no_improvement_count": 0,
            "early_stop_triggered": False,
            "early_stop_reason": "",
        }
        self._b_precheck_by_order = {}
        context = build_ga_context(state)
        self._debug_run_start(state, context, dispatch_type)

        if not context.order_ids:
            plan = self._empty_plan(dispatch_type=dispatch_type)
            self._debug_run_end(plan, None, context, started)
            return plan
        if self.config.population_size <= 0:
            plan = self._empty_plan(dispatch_type=dispatch_type, reason="population_size_not_positive")
            self._debug_run_end(plan, None, context, started)
            return plan

        self._prepare_distance_context(state)

        greedy_seed = self._build_greedy_seed(state, context)
        b_seed_rendezvous_by_order = self._build_b_precheck_and_seed_data(state, context)
        population = initialize_population(
            order_ids=context.order_ids,
            gene_pool=context.gene_pool,
            support_node_ids=context.support_node_ids,
            pop_size=self.config.population_size,
            greedy_seed=greedy_seed,
            warm_start=self._normalize_warm_start(warm_start),
            use_truck_only_seed=self.config.use_truck_only_seed,
            use_obl_seed=self.config.use_obl_seed,
            allow_c_recover_station=self.config.allow_depot_drone_recover_at_station,
            use_balanced_initialization=self.config.use_balanced_initialization,
            b_seed_rendezvous_by_order=(
                b_seed_rendezvous_by_order
                if self.config.use_b_seeded_initialization
                else None
            ),
            mutation_mode_probabilities=self._mutation_mode_probabilities(),
        )
        if not population:
            plan = self._empty_plan(dispatch_type=dispatch_type, reason="population_init_failed")
            self._debug_run_end(plan, None, context, started)
            return plan

        self._evaluate_population(population, state, context)
        self._debug_population_snapshot("initial", population)

        best_seen = math.inf
        last_improvement_gen = -1
        no_improvement_count = 0
        final_population_needs_record = False
        final_generation_index = 0

        for generation in range(self.config.generations):
            if self._timeout(started):
                break

            final_population_needs_record = False
            final_generation_index = generation
            generation_started = time.time()
            population.sort(key=lambda ind: ind.fitness)
            current_best = float(population[0].fitness)
            if current_best < best_seen - float(self.config.improvement_tolerance):
                best_seen = current_best
                last_improvement_gen = generation
                no_improvement_count = 0
            else:
                no_improvement_count += 1
            self._early_stop_info.update(
                {
                    "last_improvement_gen": last_improvement_gen,
                    "no_improvement_count": no_improvement_count,
                    "early_stop_triggered": False,
                    "early_stop_reason": "",
                }
            )

            self._record_generation(generation, population, started)
            if self._should_log_generation(generation):
                self._debug_population_snapshot(f"gen={generation}", population)

            if self._should_early_stop(generation, no_improvement_count):
                self._early_stop_info.update(
                    {
                        "early_stop_triggered": True,
                        "early_stop_reason": (
                            f"no improvement for {no_improvement_count} generations "
                            f"after min_generations={self.config.min_generations}"
                        ),
                    }
                )
                if self._evolution_rows:
                    self._evolution_rows[-1]["early_stop_triggered"] = True
                    self._evolution_rows[-1]["early_stop_reason"] = self._early_stop_info["early_stop_reason"]
                self._debug_write(
                    "early_stop "
                    f"gen={generation} "
                    f"last_improvement_gen={last_improvement_gen} "
                    f"no_improvement_count={no_improvement_count} "
                    "early_stop_triggered=True "
                    f"reason={self._early_stop_info['early_stop_reason']}"
                )
                break

            new_population: list[Individual] = []
            elite_n = max(1, int(self.config.elite_ratio * self.config.population_size))
            elite_n = min(elite_n, len(population), self.config.population_size)
            new_population.extend(copy.deepcopy(population[:elite_n]))

            while len(new_population) < self.config.population_size:
                p1 = tournament_select(population, self.config.tournament_k)
                p2 = tournament_select(population, self.config.tournament_k)

                if random.random() < self.config.crossover_rate:
                    c1, c2 = order_crossover(p1, p2)
                else:
                    c1, c2 = copy.deepcopy(p1), copy.deepcopy(p2)

                for child in (c1, c2):
                    mutation_stats = mutate(
                        child,
                        gene_pool=context.gene_pool,
                        support_node_ids=context.support_node_ids,
                        p_seq=self.config.mutation_rate_sequence,
                        p_assign=self.config.mutation_rate_assignment,
                        p_rendezvous=self.config.mutation_rate_rendezvous,
                        allow_c_recover_station=self.config.allow_depot_drone_recover_at_station,
                        mode_probabilities=self._mutation_mode_probabilities(),
                    )
                    self._accumulate_mutation_stats(mutation_stats)
                    self._evaluate_individual(child, state, context)
                    new_population.append(child)
                    if len(new_population) >= self.config.population_size:
                        break

            population = new_population
            self._last_generation_seconds = time.time() - generation_started
            final_population_needs_record = True
            final_generation_index = generation + 1

        if final_population_needs_record and population:
            population.sort(key=lambda ind: ind.fitness)
            self._record_generation(final_generation_index, population, started)

        population.sort(key=lambda ind: ind.fitness)
        best = population[0]
        self.last_best_individual = copy.deepcopy(best)
        self.last_best_decode_result = getattr(best, "decoded_result", None)
        plan = best.decoded_plan or self._empty_plan(dispatch_type=dispatch_type, reason="best_has_no_plan")
        self._annotate_plan(plan, context, started, dispatch_type)
        self._write_evolution_outputs()
        self._debug_run_end(plan, best, context, started)
        return plan

    def _evaluate_population(self, population: list[Individual], state: Any, context: Any) -> None:
        for individual in population:
            self._evaluate_individual(individual, state, context)

    def _evaluate_individual(self, individual: Individual, state: Any, context: Any) -> Any | None:
        try:
            individual.validate_with_context(
                truck_drone_ids=context.truck_drone_ids,
                depot_drone_ids=context.depot_drone_ids,
                valid_drone_ids=context.all_drone_ids,
                support_node_ids=context.support_node_ids,
            )
            result = self.decoder.decode(individual, state, context)
            individual.fitness = compute_fitness(result, self.config)
            individual.decoded_plan = result.plan
            individual.penalties = dict(result.penalties)
            setattr(individual, "decoded_result", result)
            return result
        except Exception as exc:
            individual.fitness = float(self.config.big_m)
            individual.decoded_plan = self._empty_plan(dispatch_type="ga_mmce_invalid", reason=str(exc))
            individual.penalties = {"evaluation_exception": float(self.config.weight_infeasible)}
            setattr(individual, "decoded_result", None)
            key = str(exc)
            self._debug_eval_errors[key] = self._debug_eval_errors.get(key, 0) + 1
            if self.config.verbose:
                logger.debug("[GA-MMCE] individual evaluation failed: %s", exc, exc_info=True)
            return None

    def _build_b_precheck_and_seed_data(
        self,
        state: Any,
        context: Any,
    ) -> dict[str, tuple[str, dict[str, str]]]:
        if not self.config.b_candidate_precheck:
            return {}

        result: dict[str, tuple[str, dict[str, str]]] = {}
        self._debug_write("b_candidate_precheck_start")
        for order_id in context.order_ids:
            a_candidate = self._evaluate_initial_mode_a(state, context, order_id)
            b_candidate = self._best_initial_mode_b(state, context, order_id)
            c_candidate = self._best_initial_mode_c(state, context, order_id)
            self._b_precheck_by_order[order_id] = {
                "A": self._candidate_debug_dict(a_candidate),
                "B": self._candidate_debug_dict(b_candidate),
                "C": self._candidate_debug_dict(c_candidate),
            }
            if b_candidate is not None and b_candidate.feasible and b_candidate.drone_id:
                result[order_id] = (
                    f"B_{b_candidate.drone_id}",
                    {
                        "launch": b_candidate.launch_node_id,
                        "recover": b_candidate.recover_node_id,
                    },
                )
            self._debug_write(
                "candidate_precheck "
                f"order_id={order_id} "
                f"A={self._candidate_debug_dict(a_candidate)} "
                f"B={self._candidate_debug_dict(b_candidate)} "
                f"C={self._candidate_debug_dict(c_candidate)}"
            )
        self._debug_write(f"b_candidate_precheck_seeded_orders={sorted(result)}")
        return result

    def _evaluate_initial_mode_a(self, state: Any, context: Any, order_id: str) -> Any | None:
        if not context.truck_ids:
            return None
        try:
            return self.evaluator.evaluate_fixed_mode_a(state, order_id, context.truck_ids[0])
        except Exception as exc:
            return self._exception_candidate(order_id, "A", str(exc))

    def _best_initial_mode_b(self, state: Any, context: Any, order_id: str) -> Any | None:
        if not context.truck_ids or not context.truck_drone_ids:
            return None
        best = None
        failures: Counter = Counter()
        for drone_id in context.truck_drone_ids:
            for launch in context.support_node_ids:
                for recover in context.support_node_ids:
                    try:
                        candidate = self.evaluator.evaluate_fixed_mode_b(
                            state,
                            order_id,
                            context.truck_ids[0],
                            drone_id,
                            launch,
                            recover,
                        )
                    except Exception as exc:
                        candidate = self._exception_candidate(order_id, "B", str(exc))
                    if candidate.feasible:
                        if best is None or float(candidate.score_total) < float(best.score_total):
                            best = candidate
                    else:
                        failures[candidate.reason or "infeasible"] += 1
        if best is not None:
            return best
        if failures:
            reason = failures.most_common(1)[0][0]
            candidate = self._exception_candidate(order_id, "B", reason)
            candidate.reason = reason
            return candidate
        return None

    def _best_initial_mode_c(self, state: Any, context: Any, order_id: str) -> Any | None:
        best = None
        failures: Counter = Counter()
        for drone_id in context.depot_drone_ids:
            for recover in context.support_node_ids:
                try:
                    candidate = self.evaluator.evaluate_fixed_mode_c(state, order_id, drone_id, recover)
                except Exception as exc:
                    candidate = self._exception_candidate(order_id, "C", str(exc))
                if candidate.feasible:
                    if best is None or float(candidate.score_total) < float(best.score_total):
                        best = candidate
                else:
                    failures[candidate.reason or "infeasible"] += 1
        if best is not None:
            return best
        if failures:
            reason = failures.most_common(1)[0][0]
            candidate = self._exception_candidate(order_id, "C", reason)
            candidate.reason = reason
            return candidate
        return None

    def _exception_candidate(self, order_id: str, mode: str, reason: str) -> Any:
        from .physical_evaluator import GACandidate

        return GACandidate(order_id=order_id, mode=mode, feasible=False, reason=reason or "evaluation_exception")

    def _candidate_debug_dict(self, candidate: Any | None) -> dict[str, Any]:
        if candidate is None:
            return {"feasible": False, "reason": "not_available"}
        return {
            "feasible": bool(candidate.feasible),
            "reason": candidate.reason,
            "drone_id": candidate.drone_id,
            "launch": candidate.launch_node_id,
            "recover": candidate.recover_node_id,
            "truck_dist": float(candidate.truck_distance or 0.0),
            "uav_dist": float(candidate.uav_distance or 0.0),
            "energy_cost": float(candidate.cost_energy or 0.0),
            "time_cost": float(candidate.completion_time or 0.0) * float(self.config.weight_completion),
            "sync_waiting_cost": float(candidate.waiting_time or 0.0) * float(self.config.weight_waiting),
            "closure_cost": 0.0,
            "repair_penalty": 0.0,
            "station_queue_penalty": 0.0,
            "total_score": float(candidate.score_total) if math.isfinite(float(candidate.score_total)) else math.inf,
        }

    def _build_greedy_seed(self, state: Any, context: Any) -> Individual | None:
        if not self.config.use_greedy_seed:
            return None

        greedy_plan = self._make_greedy_seed_plan(state)
        if greedy_plan is None:
            return None

        try:
            return greedy_plan_to_individual(
                greedy_plan,
                context.order_ids,
                context.gene_pool,
                context.support_node_ids,
                allow_c_recover_station=self.config.allow_depot_drone_recover_at_station,
            )
        except Exception as exc:
            logger.warning("[GA-MMCE] greedy seed 转换失败，跳过 seed: %s", exc)
            return None

    def _make_greedy_seed_plan(self, state: Any) -> Any | None:
        bbox = getattr(state, "bbox", None)
        orders = getattr(state, "orders", None)
        if not bbox or not orders:
            return None

        try:
            return self.greedy_helper.dispatch_replan_current_state(
                replan_orders=orders,
                current_time=float(getattr(state, "current_time", 0.0) or 0.0),
                bbox=bbox,
                scene_id=getattr(state, "scene_id", None),
            )
        except Exception as exc:
            logger.warning("[GA-MMCE] greedy seed 生成失败，跳过 seed: %s", exc)
            return None

    def _prepare_distance_context(self, state: Any) -> None:
        bbox = getattr(state, "bbox", None)
        if not bbox:
            return

        try:
            self.greedy_helper._road_distance_memo.clear()
            self.greedy_helper._load_road_graph(bbox, getattr(state, "scene_id", None))
        except Exception as exc:
            logger.warning("[GA-MMCE] OSM 路网预加载失败，Decoder 将 fallback 到直接距离: %s", exc)

    def _normalize_warm_start(
        self,
        warm_start: Individual | list[Individual] | None,
    ) -> list[Individual]:
        if warm_start is None:
            return [self.last_best_individual] if self.last_best_individual is not None else []
        if isinstance(warm_start, list):
            return warm_start
        return [warm_start]

    def _timeout(self, started: float) -> bool:
        if self.config.max_runtime_seconds is None:
            return False
        return time.time() - started >= self.config.max_runtime_seconds

    def _mutation_mode_probabilities(self) -> dict[str, float]:
        return {
            "A": float(self.config.mutation_mode_prob_a),
            "B": float(self.config.mutation_mode_prob_b),
            "C": float(self.config.mutation_mode_prob_c),
        }

    def _should_early_stop(self, generation: int, no_improvement_count: int) -> bool:
        patience = int(self.config.early_stopping_patience or 0)
        if patience <= 0:
            return False
        return (
            generation >= int(self.config.min_generations)
            and no_improvement_count >= patience
        )

    def _should_log_generation(self, generation: int) -> bool:
        interval = max(1, int(self.config.log_interval or 1))
        return (
            generation == 0
            or generation == self.config.generations - 1
            or generation % interval == 0
        )

    def _record_generation(self, generation: int, population: list[Individual], started: float) -> None:
        if not self.config.diagnostics_enabled:
            self._log_generation(generation, population)
            return
        row = self._build_generation_row(generation, population, started)
        self._evolution_rows.append(row)
        self._log_generation_details(row)

    def _build_generation_row(
        self,
        generation: int,
        population: list[Individual],
        started: float,
    ) -> dict[str, Any]:
        sorted_pop = sorted(population, key=lambda ind: ind.fitness)
        fitnesses = [float(ind.fitness) for ind in sorted_pop]
        best = sorted_pop[0]
        best_result = getattr(best, "decoded_result", None)
        mode_counts = self._population_mode_counts(sorted_pop)
        individual_counts = self._population_individual_mode_counts(sorted_pop)
        best_mode_counts = Counter(self._gene_mode(gene) for gene in best.assignment)
        aggregate_decode = self._aggregate_decode_diagnostics(sorted_pop)
        cost_breakdown = getattr(best_result, "cost_breakdown", {}) if best_result is not None else {}
        penalties = getattr(best_result, "penalties", {}) if best_result is not None else {}
        row = {
            "gen": generation,
            "best": fitnesses[0],
            "avg": sum(fitnesses) / len(fitnesses),
            "min_fitness": fitnesses[0],
            "median": self._median(fitnesses),
            "worst": fitnesses[-1],
            "feasible_count": sum(1 for ind in sorted_pop if not ind.penalties),
            "hard_feasible_count": sum(
                1
                for ind in sorted_pop
                if getattr(getattr(ind, "decoded_result", None), "feasible", False)
                and not getattr(getattr(ind, "decoded_result", None), "penalties", {})
            ),
            "soft_penalty_count": sum(1 for ind in sorted_pop if bool(ind.penalties)),
            "population_size": len(sorted_pop),
            "A_count": mode_counts.get("A", 0),
            "B_count": mode_counts.get("B", 0),
            "C_count": mode_counts.get("C", 0),
            "individuals_with_B": individual_counts["with_B"],
            "individuals_with_C": individual_counts["with_C"],
            "individuals_all_A": individual_counts["all_A"],
            "b_gene_count_in_population": mode_counts.get("B", 0),
            "b_individual_count": individual_counts["with_B"],
            "b_success": aggregate_decode["b_success"],
            "b_decoded_success_count": aggregate_decode["b_success"],
            "b_candidate_accepted_count": aggregate_decode["b_success"],
            "b_infeasible": aggregate_decode["b_infeasible"],
            "b_repaired": aggregate_decode["b_repaired"],
            "b_failure_reasons": aggregate_decode["b_failure_reasons"],
            "best_B_candidate_score": aggregate_decode["best_B_candidate_score"],
            "avg_B_candidate_score": aggregate_decode["avg_B_candidate_score"],
            "orders_where_B_feasible": aggregate_decode["orders_where_B_feasible"],
            "c_gene_count_in_population": mode_counts.get("C", 0),
            "c_success": aggregate_decode["c_success"],
            "c_decoded_success_count": aggregate_decode["c_success"],
            "c_candidate_accepted_count": aggregate_decode["c_success"],
            "c_infeasible": aggregate_decode["c_infeasible"],
            "c_repaired": aggregate_decode["c_repaired"],
            "c_failure_reasons": aggregate_decode["c_failure_reasons"],
            "best_A_count": best_mode_counts.get("A", 0),
            "best_B_count": best_mode_counts.get("B", 0),
            "best_C_count": best_mode_counts.get("C", 0),
            "truck_distance": cost_breakdown.get("truck_distance_cost", 0.0),
            "uav_distance": cost_breakdown.get("uav_distance_cost", 0.0),
            "energy": cost_breakdown.get("energy_cost", 0.0),
            "penalty": sum(float(value) for value in penalties.values()) if isinstance(penalties, dict) else 0.0,
            "repair_penalty": cost_breakdown.get("repair_penalty", 0.0),
            "station_queue_penalty": cost_breakdown.get("station_queue_penalty", 0.0),
            "infeasible_penalty": cost_breakdown.get("infeasible_penalty", 0.0),
            "unserved_penalty": cost_breakdown.get("unserved_penalty", 0.0),
            "elapsed": time.time() - started,
            "seconds_per_generation": self._last_generation_seconds,
            "last_improvement_gen": self._early_stop_info.get("last_improvement_gen", -1),
            "no_improvement_count": self._early_stop_info.get("no_improvement_count", 0),
            "early_stop_triggered": self._early_stop_info.get("early_stop_triggered", False),
            "early_stop_reason": self._early_stop_info.get("early_stop_reason", ""),
            "mutation_b_added": self._mutation_stats["b_added"],
            "mutation_b_removed": self._mutation_stats["b_removed"],
        }
        if best_result is not None:
            for key, value in cost_breakdown.items():
                row[key] = value
        return row

    def _log_generation_details(self, row: dict[str, Any]) -> None:
        if not self._should_log_generation(int(row["gen"])):
            return
        self._debug_write(
            "ga_evolution "
            f"gen={row['gen']} best={float(row['best']):.3f} "
            f"avg={float(row['avg']):.3f} median={float(row['median']):.3f} "
            f"worst={float(row['worst']):.3f} "
            f"feasible={row['feasible_count']}/{row['population_size']} "
            f"hard_feasible={row['hard_feasible_count']} "
            f"soft_penalty={row['soft_penalty_count']} "
            f"elapsed={float(row['elapsed']):.3f}s "
            f"sec_per_gen={float(row['seconds_per_generation']):.3f}"
        )
        self._debug_write(
            "ga_modes "
            f"gen={row['gen']} "
            f"population_gene_mode_counts={{'A': {row['A_count']}, 'B': {row['B_count']}, 'C': {row['C_count']}}} "
            f"population_individual_mode_counts={{'individuals_with_B': {row['individuals_with_B']}, "
            f"'individuals_with_C': {row['individuals_with_C']}, 'individuals_all_A': {row['individuals_all_A']}}} "
            f"best_individual_modes={{'A': {row['best_A_count']}, 'B': {row['best_B_count']}, 'C': {row['best_C_count']}}}"
        )
        self._debug_write(
            "ga_b_diagnostics "
            f"gen={row['gen']} b_gene_count_in_population={row['b_gene_count_in_population']} "
            f"b_individual_count={row['b_individual_count']} "
            f"b_decoded_success_count={row['b_decoded_success_count']} "
            f"b_candidate_accepted_count={row['b_candidate_accepted_count']} "
            f"b_repaired_count={row['b_repaired']} "
            f"b_infeasible_count={row['b_infeasible']} "
            f"b_failure_reasons={row['b_failure_reasons']} "
            f"best_B_candidate_score={row['best_B_candidate_score']} "
            f"avg_B_candidate_score={row['avg_B_candidate_score']} "
            f"orders_where_B_feasible={row['orders_where_B_feasible']}"
        )
        self._debug_write(
            "ga_c_diagnostics "
            f"gen={row['gen']} c_gene_count_in_population={row['c_gene_count_in_population']} "
            f"c_decoded_success_count={row['c_decoded_success_count']} "
            f"c_candidate_accepted_count={row['c_candidate_accepted_count']} "
            f"c_repaired_count={row['c_repaired']} "
            f"c_infeasible_count={row['c_infeasible']} "
            f"c_failure_reasons={row['c_failure_reasons']}"
        )
        self._debug_write(
            "ga_cost_breakdown "
            f"gen={row['gen']} "
            f"truck_distance_cost={row.get('truck_distance_cost', 0.0)} "
            f"uav_distance_cost={row.get('uav_distance_cost', 0.0)} "
            f"energy_cost={row.get('energy_cost', 0.0)} "
            f"time_cost={row.get('time_cost', 0.0)} "
            f"waiting_cost={row.get('waiting_cost', 0.0)} "
            f"closure_route_cost={row.get('closure_route_cost', 0.0)} "
            f"repair_penalty={row.get('repair_penalty', 0.0)} "
            f"station_queue_penalty={row.get('station_queue_penalty', 0.0)} "
            f"unserved_penalty={row.get('unserved_penalty', 0.0)} "
            f"total_fitness={row.get('total_fitness', row['best'])}"
        )
        self._debug_write(
            "ga_early_stop "
            f"gen={row['gen']} last_improvement_gen={row['last_improvement_gen']} "
            f"no_improvement_count={row['no_improvement_count']} "
            f"early_stop_triggered={row['early_stop_triggered']} "
            f"early_stop_reason={row['early_stop_reason']}"
        )

    def _write_evolution_outputs(self) -> None:
        if not self.config.diagnostics_enabled or not self._evolution_rows:
            return
        log_dir = self._diagnostics_dir()
        try:
            if self.config.save_evolution_csv:
                write_evolution_csv(self._evolution_rows, log_dir / "ga_evolution_static.csv")
            if self.config.save_evolution_plots:
                write_evolution_plots(self._evolution_rows, log_dir)
        except Exception as exc:
            self._debug_write(f"diagnostics_output_failed reason={exc}")

    def _diagnostics_dir(self) -> Path:
        configured = Path(str(self.config.diagnostics_dir or "logs"))
        return configured if configured.is_absolute() else PROJECT_ROOT / configured

    def _population_mode_counts(self, population: list[Individual]) -> Counter:
        counts: Counter = Counter()
        for ind in population:
            counts.update(self._gene_mode(gene) for gene in ind.assignment)
        return counts

    def _population_individual_mode_counts(self, population: list[Individual]) -> dict[str, int]:
        result = {"with_B": 0, "with_C": 0, "all_A": 0}
        for ind in population:
            modes = {self._gene_mode(gene) for gene in ind.assignment}
            if "B" in modes:
                result["with_B"] += 1
            if "C" in modes:
                result["with_C"] += 1
            if modes == {"A"}:
                result["all_A"] += 1
        return result

    def _aggregate_decode_diagnostics(self, population: list[Individual]) -> dict[str, Any]:
        aggregate = {
            "b_success": 0,
            "b_infeasible": 0,
            "b_repaired": 0,
            "b_failure_reasons": {},
            "best_B_candidate_score": math.inf,
            "avg_B_candidate_score": math.inf,
            "orders_where_B_feasible": [],
            "c_success": 0,
            "c_infeasible": 0,
            "c_repaired": 0,
            "c_failure_reasons": {},
        }
        b_scores: list[float] = []
        b_orders: set[str] = set()
        for ind in population:
            result = getattr(ind, "decoded_result", None)
            diagnostics = getattr(result, "diagnostics", {}) if result is not None else {}
            aggregate["b_success"] += int(diagnostics.get("b_decoded_success_count", 0) or 0)
            aggregate["b_infeasible"] += int(diagnostics.get("b_infeasible_count", 0) or 0)
            aggregate["b_repaired"] += int(diagnostics.get("b_repaired_count", 0) or 0)
            aggregate["c_success"] += int(diagnostics.get("c_decoded_success_count", 0) or 0)
            aggregate["c_infeasible"] += int(diagnostics.get("c_infeasible_count", 0) or 0)
            aggregate["c_repaired"] += int(diagnostics.get("c_repaired_count", 0) or 0)
            self._merge_counts(aggregate["b_failure_reasons"], diagnostics.get("b_failure_reasons", {}))
            self._merge_counts(aggregate["c_failure_reasons"], diagnostics.get("c_failure_reasons", {}))
            score = float(diagnostics.get("best_B_candidate_score", math.inf) or math.inf)
            if math.isfinite(score):
                b_scores.append(score)
            for order_id in diagnostics.get("orders_where_B_feasible", []) or []:
                b_orders.add(str(order_id))
        if b_scores:
            aggregate["best_B_candidate_score"] = min(b_scores)
            aggregate["avg_B_candidate_score"] = sum(b_scores) / len(b_scores)
        aggregate["orders_where_B_feasible"] = sorted(b_orders)
        return aggregate

    def _merge_counts(self, target: dict[str, int], source: Any) -> None:
        if not isinstance(source, dict):
            return
        for key, value in source.items():
            target[str(key)] = target.get(str(key), 0) + int(value)

    def _median(self, values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def _gene_mode(self, gene: str) -> str:
        if gene == "A":
            return "A"
        if gene.startswith("B_"):
            return "B"
        if gene.startswith("C_"):
            return "C"
        return "?"

    def _empty_mutation_stats(self) -> dict[str, Any]:
        return {"assignment_mutations": 0, "b_added": 0, "b_removed": 0, "by_transition": {}}

    def _accumulate_mutation_stats(self, mutation_stats: Any) -> None:
        self._mutation_stats["assignment_mutations"] += int(getattr(mutation_stats, "assignment_mutations", 0) or 0)
        self._mutation_stats["b_added"] += int(getattr(mutation_stats, "b_added", 0) or 0)
        self._mutation_stats["b_removed"] += int(getattr(mutation_stats, "b_removed", 0) or 0)
        for key, value in getattr(mutation_stats, "by_transition", {}).items():
            bucket = self._mutation_stats["by_transition"]
            bucket[key] = bucket.get(key, 0) + int(value)

    def _log_generation(self, generation: int, population: list[Individual]) -> None:
        if not self.config.verbose or not self._should_log_generation(generation):
            return

        best = min(population, key=lambda ind: ind.fitness)
        avg = sum(float(ind.fitness) for ind in population) / max(1, len(population))
        feasible_count = sum(1 for ind in population if not ind.penalties)
        logger.info(
            "[GA-MMCE] gen=%s best=%.2f avg=%.2f feasible=%s penalties=%s",
            generation,
            best.fitness,
            avg,
            feasible_count,
            best.penalties,
        )

    def _annotate_plan(
        self,
        plan: DispatchPlan,
        context: Any,
        started: float,
        dispatch_type: str,
    ) -> None:
        modes: dict[str, int] = {}
        for allocation in plan.allocations:
            modes[allocation.mode] = modes.get(allocation.mode, 0) + 1

        summary = plan.summary
        summary["ga_feasible"] = bool(summary.get("feasible", False))
        summary["total_orders"] = len(context.order_ids)
        summary["feasible"] = sum(1 for allocation in plan.allocations if allocation.feasible)
        summary["modes"] = modes
        summary["dispatch_type"] = dispatch_type
        summary["solver"] = "ga_mmce"
        summary["runtime_seconds"] = time.time() - started
        summary["best_fitness"] = float(plan.cost_total or 0.0)
        full_breakdown = dict(summary.get("cost_breakdown", {}) or {})
        summary["cost_breakdown_detail"] = full_breakdown
        summary["cost_breakdown"] = {
            "dist": sum(float(allocation.cost_dist or 0.0) for allocation in plan.allocations),
            "energy": sum(float(allocation.cost_energy or 0.0) for allocation in plan.allocations),
            "penalty": float(summary.get("total_penalty_cost", 0.0) or 0.0),
        }
        summary["early_stopping"] = dict(self._early_stop_info)
        summary["b_candidate_precheck"] = dict(self._b_precheck_by_order)

    def _empty_plan(self, dispatch_type: str = "ga_mmce", reason: str = "") -> DispatchPlan:
        summary = {
            "total_orders": 0,
            "feasible": 0,
            "modes": {},
            "dispatch_type": dispatch_type,
            "solver": "ga_mmce",
            "cost_breakdown": {"dist": 0.0, "energy": 0.0, "penalty": 0.0},
        }
        if reason:
            summary["reason"] = reason
        return DispatchPlan(allocations=[], cost_total=0.0, summary=summary)

    def _make_greedy_helper(self, entity_mgr: Any) -> Any:
        try:
            from ..greedy_mmce import GreedyMMCE
        except Exception:
            try:
                from greedy_mmce import GreedyMMCE
            except Exception:
                from solver.greedy_mmce import GreedyMMCE
        return GreedyMMCE(entity_mgr)

    def _debug_run_start(self, state: Any, context: Any, dispatch_type: str) -> None:
        self._debug_write("")
        self._debug_write("=" * 100)
        self._debug_write(
            "RUN START "
            f"dispatch_type={dispatch_type} "
            f"orders={len(context.order_ids)} "
            f"current_time={float(getattr(state, 'current_time', 0.0) or 0.0):.2f}"
        )
        self._debug_write(f"order_ids={context.order_ids}")
        self._debug_write(f"truck_ids={context.truck_ids} depot_ids={context.depot_ids} station_ids={context.station_ids}")
        self._debug_write(f"truck_drone_ids={context.truck_drone_ids}")
        self._debug_write(f"depot_drone_ids={context.depot_drone_ids}")
        self._debug_write(f"all_drone_ids={context.all_drone_ids}")
        self._debug_write(f"gene_pool={context.gene_pool}")
        self._debug_write(f"support_node_ids={context.support_node_ids}")
        self._debug_write(
            "ga_config "
            f"max_generations={self.config.generations} "
            f"population_size={self.config.population_size} "
            f"min_generations={self.config.min_generations} "
            f"early_stopping_patience={self.config.early_stopping_patience} "
            f"improvement_tolerance={self.config.improvement_tolerance} "
            f"log_interval={self.config.log_interval} "
            f"mode_mutation_probabilities={self._mutation_mode_probabilities()} "
            f"balanced_initialization={self.config.use_balanced_initialization} "
            f"b_seeded_initialization={self.config.use_b_seeded_initialization}"
        )

    def _debug_population_snapshot(self, label: str, population: list[Individual]) -> None:
        if not population:
            self._debug_write(f"{label}: population empty")
            return

        sorted_pop = sorted(population, key=lambda ind: ind.fitness)
        best = sorted_pop[0]
        avg = sum(float(ind.fitness) for ind in sorted_pop) / len(sorted_pop)
        feasible_individuals = sum(1 for ind in sorted_pop if not ind.penalties)
        mode_counts = self._population_mode_counts(sorted_pop)
        individual_counts = self._population_individual_mode_counts(sorted_pop)
        self._debug_write(
            f"{label}: best={float(best.fitness):.3f} avg={avg:.3f} "
            f"feasible_individuals={feasible_individuals}/{len(sorted_pop)} "
            f"best_penalties={best.penalties}"
        )
        self._debug_write(
            f"{label}: mode_counts={{'A': {mode_counts.get('A', 0)}, "
            f"'B': {mode_counts.get('B', 0)}, 'C': {mode_counts.get('C', 0)}}} "
            f"individual_counts={individual_counts}"
        )

        result = getattr(best, "decoded_result", None)
        if result is not None:
            self._debug_write(
                f"{label}: decode feasible={result.feasible} "
                f"objective={result.objective:.3f} "
                f"penalties={result.penalties} "
                f"penalty_counts={result.penalty_counts} "
                f"unserved={result.unserved_order_ids}"
            )
            self._debug_write(f"{label}: metrics={result.metrics}")
        elif self._debug_eval_errors:
            self._debug_write(f"{label}: eval_errors={self._debug_eval_errors}")

    def _debug_run_end(
        self,
        plan: DispatchPlan,
        best: Individual | None,
        context: Any,
        started: float,
    ) -> None:
        self._debug_write(
            "RUN END "
            f"elapsed={time.time() - started:.3f}s "
            f"plan_cost={float(plan.cost_total or 0.0):.3f} "
            f"summary={plan.summary}"
        )
        if best is not None:
            self._debug_write(f"best_sequence={best.sequence}")
            self._debug_write(f"best_assignment={best.assignment}")
            self._debug_write(f"best_rendezvous={best.rendezvous}")
            result = getattr(best, "decoded_result", None)
            if result is not None:
                self._debug_write(f"best_penalty_counts={result.penalty_counts}")
                self._debug_write(f"best_unserved_order_ids={result.unserved_order_ids}")
                self._debug_write(f"best_candidate_count={len(result.candidates)}")
                for candidate in result.candidates[:20]:
                    self._debug_write(
                        "candidate "
                        f"order={candidate.order_id} mode={candidate.mode} "
                        f"truck={candidate.truck_id} drone={candidate.drone_id} "
                        f"launch={candidate.launch_node_id} recover={candidate.recover_node_id} "
                        f"score={candidate.score_total:.3f} "
                        f"truck_dist={candidate.truck_distance:.1f} "
                        f"uav_dist={candidate.uav_distance:.1f}"
                    )
        if self._debug_eval_errors:
            self._debug_write(f"evaluation_exceptions={self._debug_eval_errors}")
        self._debug_write("=" * 100)

    def _debug_write(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"{timestamp} {message}\n")
        except Exception:
            logger.debug("[GA-MMCE] failed to write debug log", exc_info=True)
