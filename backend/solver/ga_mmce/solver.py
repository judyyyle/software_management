from __future__ import annotations

import copy
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .adapters import build_ga_context, greedy_plan_to_individual
from .chromosome import Individual
from .config import GAConfig
from .decoder import DispatchPlan, GADecoder
from .fitness import compute_fitness
from .operators import mutate, order_crossover, tournament_select
from .physical_evaluator import PhysicalEvaluator
from .population import initialize_population


logger = logging.getLogger(__name__)


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
        context = build_ga_context(state)

        if not context.order_ids:
            return self._empty_plan(dispatch_type=dispatch_type)
        if self.config.population_size <= 0:
            return self._empty_plan(dispatch_type=dispatch_type, reason="population_size_not_positive")

        self._prepare_distance_context(state)

        greedy_seed = self._build_greedy_seed(state, context)
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
        )
        if not population:
            return self._empty_plan(dispatch_type=dispatch_type, reason="population_init_failed")

        self._evaluate_population(population, state, context)

        for generation in range(self.config.generations):
            if self._timeout(started):
                break

            population.sort(key=lambda ind: ind.fitness)
            self._log_generation(generation, population)

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
                    mutate(
                        child,
                        gene_pool=context.gene_pool,
                        support_node_ids=context.support_node_ids,
                        p_seq=self.config.mutation_rate_sequence,
                        p_assign=self.config.mutation_rate_assignment,
                        p_rendezvous=self.config.mutation_rate_rendezvous,
                        allow_c_recover_station=self.config.allow_depot_drone_recover_at_station,
                    )
                    self._evaluate_individual(child, state, context)
                    new_population.append(child)
                    if len(new_population) >= self.config.population_size:
                        break

            population = new_population

        population.sort(key=lambda ind: ind.fitness)
        best = population[0]
        self.last_best_individual = copy.deepcopy(best)
        self.last_best_decode_result = getattr(best, "decoded_result", None)
        plan = best.decoded_plan or self._empty_plan(dispatch_type=dispatch_type, reason="best_has_no_plan")
        self._annotate_plan(plan, context, started, dispatch_type)
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
            if self.config.verbose:
                logger.debug("[GA-MMCE] individual evaluation failed: %s", exc, exc_info=True)
            return None

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

    def _log_generation(self, generation: int, population: list[Individual]) -> None:
        if not self.config.verbose or generation % 10 != 0:
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
        summary["cost_breakdown"] = {
            "dist": sum(float(allocation.cost_dist or 0.0) for allocation in plan.allocations),
            "energy": sum(float(allocation.cost_energy or 0.0) for allocation in plan.allocations),
            "penalty": float(summary.get("total_penalty_cost", 0.0) or 0.0),
        }

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
