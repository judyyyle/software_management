#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 求解器接口定义。

用于将调度编排层（DecisionEngine）与具体算法实现解耦。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from solver.greedy_baseline import DispatchPlan

if TYPE_CHECKING:
    from core.entities.order import Order


class DispatchSolver(Protocol):
    """调度求解器协议。"""

    def dispatch(
        self,
        pending_orders: dict[str, "Order"],
        current_time: float,
        bbox: dict,
        scene_id: str | None = None,
    ) -> DispatchPlan:
        """执行一轮求解并返回标准化 DispatchPlan。"""
        ...
