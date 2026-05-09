#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TrainingEnvAdapter 前端在线运行时适配器测试。
"""

from __future__ import annotations

import time
import unittest

from training.env_adapter import TrainingEnvAdapter
from training.frontend_runtime_adapter import (
    TrainingTelemetryBridge,
    build_orders_raw_from_sim_init_payload,
    resolve_scene_context,
)
from training.order_source_adapter import OrderSourceMode, build_order_source
from training.scene_loader import DEFAULT_CONFIG_PATH


class TestFrontendRuntimeAdapter(unittest.TestCase):
    def test_build_orders_raw_from_sim_init_payload_normalizes_wall_ms_initial_orders(self) -> None:
        orders_raw = build_orders_raw_from_sim_init_payload(
            initial_orders=[
                {
                    "order_id": "ORD-1",
                    "create_time": 1_710_000_000_000,
                    "deadline": 1_710_000_060_000,
                    "delivery_lng": 121.20,
                    "delivery_lat": 31.03,
                    "delivery_z": 0,
                    "payload_weight": 1.5,
                    "source_type": "DEPOT",
                    "pickup_source_id": None,
                    "time_domain": "wall_ms",
                },
                {
                    "order_id": "ORD-2",
                    "create_time": 1_710_000_030_000,
                    "deadline": 1_710_000_120_000,
                    "delivery_lng": 121.21,
                    "delivery_lat": 31.04,
                    "delivery_z": 0,
                    "payload_weight": 2.0,
                    "source_type": "DEPOT",
                    "pickup_source_id": None,
                    "time_domain": "wall_ms",
                },
            ],
            scheduled_dynamic_orders=[],
        )

        self.assertEqual(orders_raw["static_orders"], [])
        dynamic_orders = orders_raw["dynamic_orders"]
        self.assertEqual(len(dynamic_orders), 2)
        self.assertEqual(dynamic_orders[0]["order_id"], "ORD-1")
        self.assertAlmostEqual(float(dynamic_orders[0]["spawn_sim_s"]), 0.0, places=6)
        self.assertAlmostEqual(float(dynamic_orders[0]["deadline_sim_s"]), 60.0, places=6)
        self.assertAlmostEqual(float(dynamic_orders[1]["spawn_sim_s"]), 30.0, places=6)
        self.assertAlmostEqual(float(dynamic_orders[1]["deadline_sim_s"]), 120.0, places=6)

    def test_training_telemetry_bridge_builds_frontend_payload(self) -> None:
        scene_ctx = resolve_scene_context(config_path=DEFAULT_CONFIG_PATH)
        order_source = build_order_source(
            scene_ctx,
            mode=OrderSourceMode.BENCHMARK,
            config_path=DEFAULT_CONFIG_PATH,
        )
        env = TrainingEnvAdapter(
            scene_ctx=scene_ctx,
            order_source=order_source,
            config_path=DEFAULT_CONFIG_PATH,
        )
        env.reset()

        bridge = TrainingTelemetryBridge(
            policy_name="test_policy",
            checkpoint_path="backend/weights/test/policy.pt",
        )
        tick = bridge.build_tick_payload(
            env=env,
            speed_ratio=1.0,
            deterministic=True,
            order_source_mode="benchmark",
        )
        snapshot = bridge.build_full_snapshot(
            env=env,
            is_running=False,
            speed_ratio=1.0,
            sim_start_wall_ms=int(time.time() * 1000),
            deterministic=True,
            order_source_mode="benchmark",
        )

        self.assertIn("sim_time", tick)
        self.assertIn("entities", tick)
        self.assertIn("orders", tick)
        self.assertIn("stats", tick)
        self.assertEqual(len(tick["entities"]["trucks"]), 1)
        self.assertEqual(len(tick["entities"]["drones"]), len(scene_ctx.drones))
        self.assertEqual(len(tick["entities"]["stations"]), len(scene_ctx.stations))
        self.assertEqual(tick["stats"]["active_policy"], "test_policy")
        self.assertIn("current_decision", tick["stats"])
        self.assertEqual(snapshot["type"], "FULL_SNAPSHOT")
        self.assertIn("payload", snapshot)
        self.assertIn("is_running", snapshot["payload"])
        self.assertIn("sim_start_wall_ms", snapshot["payload"])


if __name__ == "__main__":
    unittest.main()
