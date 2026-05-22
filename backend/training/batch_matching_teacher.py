#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch matching teacher for behavior-cloning labels.

This module is read-only with respect to the environment.  It consumes a
same-time decision batch plus the corresponding CandidateOutput objects and
returns per-UAV BC labels/actions.  It does not call env.step(), does not submit
actions, and does not advance simulation time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .actions import DispatchAction, EnvAction, WAIT_ACTION
from .contracts import CandidateOutput, ResolvedActionIndices


_INF = 1.0e18
_TIME_EPS = 1.0e-6
_DEFAULT_WAIT_DISPATCH_MARGIN = 1_000_000.0
_MODE_C_WAIT_COST_WEIGHT = 0.50


DispatchCostFn = Callable[[Any, CandidateOutput, DispatchAction, int, int], float]
WaitCostFn = Callable[[Any, CandidateOutput, float | None], float]


@dataclass(frozen=True)
class BatchTeacherAssignment:
    """Teacher choice for one UAV in a same-time decision batch."""

    drone_id: str
    action: EnvAction
    action_indices: ResolvedActionIndices
    cost: float


@dataclass(frozen=True)
class BatchMatchingTeacherResult:
    """Batch teacher output for BC training."""

    assignments_by_drone: Mapping[str, BatchTeacherAssignment]
    labels_by_drone: Mapping[str, ResolvedActionIndices]
    actions_by_drone: Mapping[str, EnvAction]
    total_cost: float


@dataclass(frozen=True)
class _DispatchChoice:
    order_id: str
    action: DispatchAction
    action_indices: ResolvedActionIndices
    cost: float


def build_batch_matching_teacher_labels(
    *,
    decision_contexts: tuple[Any, ...],
    candidate_outputs_by_drone: Mapping[str, CandidateOutput],
    dispatch_cost_fn: DispatchCostFn | None = None,
    wait_cost_fn: WaitCostFn | None = None,
    require_shared_snapshot: bool = True,
) -> BatchMatchingTeacherResult:
    """Build mutually-exclusive per-UAV BC labels for a same-time batch.

    Constraints enforced:
      - each UAV receives exactly one label;
      - each order can be assigned to at most one UAV;
      - WAIT is represented by one private dummy column per UAV.
    """

    if not decision_contexts:
        return BatchMatchingTeacherResult(
            assignments_by_drone={},
            labels_by_drone={},
            actions_by_drone={},
            total_cost=0.0,
        )

    _validate_contexts(decision_contexts, require_shared_snapshot=require_shared_snapshot)
    dispatch_cost = dispatch_cost_fn or _default_dispatch_cost
    wait_cost = wait_cost_fn or _default_wait_cost

    drone_ids = [str(context.deciding_drone_id) for context in decision_contexts]
    if len(set(drone_ids)) != len(drone_ids):
        raise ValueError(f"decision batch 中存在重复 UAV: {drone_ids}")

    missing = [drone_id for drone_id in drone_ids if drone_id not in candidate_outputs_by_drone]
    if missing:
        raise ValueError(f"candidate_outputs_by_drone 缺少 UAV: {missing}")

    choices_by_drone: dict[str, dict[str, _DispatchChoice]] = {}
    ordered_order_ids: list[str] = []
    seen_order_ids: set[str] = set()
    wait_cost_by_drone: dict[str, float] = {}

    for context in decision_contexts:
        drone_id = str(context.deciding_drone_id)
        candidate_out = candidate_outputs_by_drone[drone_id]
        dispatch_choices = _best_dispatch_choice_by_order(
            context=context,
            candidate_out=candidate_out,
            dispatch_cost_fn=dispatch_cost,
        )
        choices_by_drone[drone_id] = dispatch_choices
        for order_id in sorted(dispatch_choices):
            if order_id not in seen_order_ids:
                seen_order_ids.add(order_id)
                ordered_order_ids.append(order_id)

        best_cost = (
            min(choice.cost for choice in dispatch_choices.values())
            if dispatch_choices
            else None
        )
        if candidate_out.root_branch_mask[0] and candidate_out.has_wait_action:
            cost = float(wait_cost(context, candidate_out, best_cost))
            if not math.isfinite(cost):
                raise ValueError(f"WAIT cost 非有限值: drone_id={drone_id}, cost={cost}")
            wait_cost_by_drone[drone_id] = cost

    columns: list[tuple[str, str]] = [
        ("DISPATCH", order_id) for order_id in ordered_order_ids
    ]
    columns.extend(("WAIT", drone_id) for drone_id in drone_ids)

    cost_matrix: list[list[float]] = []
    for drone_id in drone_ids:
        row: list[float] = []
        for kind, key in columns:
            if kind == "DISPATCH":
                choice = choices_by_drone[drone_id].get(key)
                row.append(_INF if choice is None else float(choice.cost))
            elif key == drone_id and drone_id in wait_cost_by_drone:
                row.append(wait_cost_by_drone[drone_id])
            else:
                row.append(_INF)
        cost_matrix.append(row)

    selected_columns = _solve_rectangular_assignment(cost_matrix)
    assignments: dict[str, BatchTeacherAssignment] = {}
    total_cost = 0.0
    for row_idx, col_idx in enumerate(selected_columns):
        drone_id = drone_ids[row_idx]
        kind, key = columns[col_idx]
        selected_cost = float(cost_matrix[row_idx][col_idx])
        if not math.isfinite(selected_cost) or selected_cost >= _INF / 2.0:
            raise RuntimeError(f"无法为 UAV {drone_id} 求得合法 teacher 动作")

        if kind == "WAIT":
            action = WAIT_ACTION
            action_indices = ResolvedActionIndices(root_branch_idx=0)
        else:
            choice = choices_by_drone[drone_id][key]
            action = choice.action
            action_indices = choice.action_indices

        assignments[drone_id] = BatchTeacherAssignment(
            drone_id=drone_id,
            action=action,
            action_indices=action_indices,
            cost=selected_cost,
        )
        total_cost += selected_cost

    return BatchMatchingTeacherResult(
        assignments_by_drone=assignments,
        labels_by_drone={
            drone_id: assignment.action_indices
            for drone_id, assignment in assignments.items()
        },
        actions_by_drone={
            drone_id: assignment.action
            for drone_id, assignment in assignments.items()
        },
        total_cost=float(total_cost),
    )


def _validate_contexts(
    decision_contexts: tuple[Any, ...],
    *,
    require_shared_snapshot: bool,
) -> None:
    first = decision_contexts[0]
    first_time = float(first.t_decision)
    runtime_state = first.runtime_state
    coarse_plan = first.coarse_plan
    for context in decision_contexts:
        if abs(float(context.t_decision) - first_time) > _TIME_EPS:
            raise ValueError("decision_contexts 混入了不同 t_decision")
        if require_shared_snapshot and context.runtime_state is not runtime_state:
            raise ValueError("decision_contexts 必须共享同一份 runtime_state snapshot")
        if require_shared_snapshot and context.coarse_plan is not coarse_plan:
            raise ValueError("decision_contexts 必须共享同一份 coarse_plan snapshot")


def _best_dispatch_choice_by_order(
    *,
    context: Any,
    candidate_out: CandidateOutput,
    dispatch_cost_fn: DispatchCostFn,
) -> dict[str, _DispatchChoice]:
    best_by_order: dict[str, _DispatchChoice] = {}
    for (order_idx, mode_idx), action in sorted(
        candidate_out.resolved_action_lookup.dispatch_actions.items()
    ):
        cost = float(dispatch_cost_fn(context, candidate_out, action, order_idx, mode_idx))
        if not math.isfinite(cost):
            continue
        order_id = str(action.order_id)
        candidate = _DispatchChoice(
            order_id=order_id,
            action=action,
            action_indices=ResolvedActionIndices(
                root_branch_idx=1,
                order_idx=int(order_idx),
                mode_idx=int(mode_idx),
            ),
            cost=cost,
        )
        previous = best_by_order.get(order_id)
        if previous is None or (candidate.cost, candidate.action.mode) < (
            previous.cost,
            previous.action.mode,
        ):
            best_by_order[order_id] = candidate
    return best_by_order


def _default_dispatch_cost(
    context: Any,
    candidate_out: CandidateOutput,
    action: DispatchAction,
    order_idx: int,
    mode_idx: int,
) -> float:
    del context, mode_idx
    order_feature = candidate_out.candidate_features.order_features[order_idx]

    mode = str(action.mode)
    if mode == "B":
        return float(order_feature.best_mode_b_recovery_flight_time)
    if mode == "C":
        return (
            float(order_feature.best_mode_c_uav_flight_time)
            + _MODE_C_WAIT_COST_WEIGHT
            * float(order_feature.best_mode_c_wait_time)
        )
    raise ValueError(f"未知 dispatch mode: {action.mode}")


def _default_wait_cost(
    _context: Any,
    _candidate_out: CandidateOutput,
    best_dispatch_cost: float | None,
) -> float:
    if best_dispatch_cost is None:
        return 0.0
    return float(best_dispatch_cost) + _DEFAULT_WAIT_DISPATCH_MARGIN


def _solve_rectangular_assignment(cost_matrix: list[list[float]]) -> list[int]:
    """Return selected column index for each row using Hungarian minimization."""

    row_count = len(cost_matrix)
    if row_count == 0:
        return []
    col_count = len(cost_matrix[0])
    if col_count < row_count:
        raise ValueError(
            f"assignment 矩阵列数必须不少于行数: rows={row_count}, cols={col_count}"
        )
    if any(len(row) != col_count for row in cost_matrix):
        raise ValueError("assignment 矩阵行长度不一致")

    u = [0.0] * (row_count + 1)
    v = [0.0] * (col_count + 1)
    p = [0] * (col_count + 1)
    way = [0] * (col_count + 1)

    for i in range(1, row_count + 1):
        p[0] = i
        j0 = 0
        minv = [math.inf] * (col_count + 1)
        used = [False] * (col_count + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = math.inf
            j1 = 0
            for j in range(1, col_count + 1):
                if used[j]:
                    continue
                cur = float(cost_matrix[i0 - 1][j - 1]) - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            if not math.isfinite(delta):
                raise RuntimeError("assignment 矩阵不存在完整可行匹配")
            for j in range(0, col_count + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * row_count
    for j in range(1, col_count + 1):
        if p[j] != 0:
            assignment[p[j] - 1] = j - 1
    if any(col_idx < 0 for col_idx in assignment):
        raise RuntimeError("assignment 求解失败")
    return assignment


__all__ = [
    "BatchMatchingTeacherResult",
    "BatchTeacherAssignment",
    "DispatchCostFn",
    "WaitCostFn",
    "build_batch_matching_teacher_labels",
]
