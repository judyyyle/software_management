#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 离线训练子包。

当前阶段先固化跨模块共享的训练期契约类型，后续逐步补充：
  - scene_loader
  - order_source_adapter
  - planner_bridge
  - candidate_builder
  - env_adapter
"""

from .scene_loader import (
    BenchmarkDynamicOrder,
    TrainingRoadNetwork,
    TrainingSceneContext,
    load_default_scene,
    load_training_scene,
)
from .order_source_adapter import (
    OrderSourceConfig,
    OrderSourceMode,
    PoissonOrderGenConfig,
    build_order_source,
    build_order_source_preview_summary,
    configure_order_manager_for_source,
    ensure_mode_allowed,
    preview_dynamic_order_stream,
)

__all__ = [
    "BenchmarkDynamicOrder",
    "OrderSourceConfig",
    "OrderSourceMode",
    "PoissonOrderGenConfig",
    "TrainingRoadNetwork",
    "TrainingSceneContext",
    "build_order_source",
    "build_order_source_preview_summary",
    "configure_order_manager_for_source",
    "ensure_mode_allowed",
    "load_default_scene",
    "load_training_scene",
    "preview_dynamic_order_stream",
]
