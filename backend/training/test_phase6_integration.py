#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 6 integration tests.

运行方式：
  python -m unittest backend.training.test_phase6_integration
"""

from __future__ import annotations

import math
import unittest

from core.entities.order import Order
from core.entities.primitives import Position3D

from .contracts import PolicyMode
from .env_adapter import BackboneVisit, TrainingDroneState, TrainingEnvAdapter, WAIT_ACTION


class TestPhase6Integration(unittest.TestCase):
    def _make_env(self) -> TrainingEnvAdapter:
        return TrainingEnvAdapter()

    def _first_idle_drone_id(self, env: TrainingEnvAdapter) -> str:
        for drone_id, state in env._drone_state.items():
            if state == TrainingDroneState.IDLE:
                return drone_id
        self.fail("默认场景中至少应有一架 idle 无人机")

    def _reset_controlled_env(self) -> tuple[TrainingEnvAdapter, str]:
        env = self._make_env()
        env.reset()

        order_mgr = env._require_order_manager()
        order_mgr.pending_orders.clear()
        order_mgr.assigned_orders.clear()
        order_mgr.completed_orders.clear()
        order_mgr._next_order_time = math.inf
        order_mgr._scheduled_dynamic = []
        order_mgr._scheduled_dynamic_i = 0

        env._decision_queue.clear()
        env._background_mode_a_pending.clear()
        drone_id = self._first_idle_drone_id(env)
        return env, drone_id

    def _inject_order_at(
        self,
        env: TrainingEnvAdapter,
        *,
        order_id: str,
        position: Position3D,
        deadline_offset: float = 3600.0,
        payload_weight: float = 1.0,
    ) -> Order:
        order = Order(
            order_id=order_id,
            create_time=env._t_now,
            deadline=env._t_now + deadline_offset,
            delivery_loc=Position3D(x=position.x, y=position.y, z=position.z),
            payload_weight=payload_weight,
        )
        env._require_order_manager().pending_orders[order.order_id] = order
        return order

    def test_stale_cached_plan_does_not_suppress_new_idle_decision(self) -> None:
        env, drone_id = self._reset_controlled_env()
        drone = env._require_entity_manager().drones[drone_id]

        stale_order = self._inject_order_at(
            env,
            order_id="ORDER-P6-STALE-01",
            position=Position3D(
                x=drone.current_loc.x + 20.0,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
        )
        runtime_state = env.build_runtime_state_view()
        self.assertTrue(env._has_authorized_orders_now(runtime_state))

        stale_plan = env._current_coarse_plan
        self.assertIsNotNone(stale_plan)
        self.assertTrue(stale_plan.is_order_authorized(stale_order.order_id))
        stale_plan_version = stale_plan.plan_version

        env._require_order_manager().pending_orders.clear()
        new_order = self._inject_order_at(
            env,
            order_id="ORDER-P6-STALE-02",
            position=Position3D(
                x=drone.current_loc.x + 40.0,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
        )
        self.assertFalse(
            stale_plan.is_order_authorized(new_order.order_id),
            "测试前置条件不成立：旧 coarse plan 不应提前包含新订单",
        )

        env._enqueue_initial_idle_decisions()

        refreshed_plan = env._current_coarse_plan
        self.assertIsNotNone(refreshed_plan)
        self.assertGreater(
            refreshed_plan.plan_version,
            stale_plan_version,
            "当旧 coarse plan 不含新订单时，应触发 replan 而不是继续沿用旧缓存",
        )
        self.assertTrue(refreshed_plan.is_order_authorized(new_order.order_id))
        self.assertFalse(refreshed_plan.is_order_authorized(stale_order.order_id))

        self.assertTrue(
            any(
                trigger.drone_id == drone_id and trigger.trigger_type == "initial_idle"
                for trigger in env._decision_queue
            ),
            "新单出现时，不应因为旧 coarse plan 仍缓存而压住 idle 决策点",
        )

    def test_planner_bridge_resets_active_launch_stations_on_replan(self) -> None:
        env, _drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        station_ids = sorted(entity_mgr.stations)
        self.assertGreaterEqual(len(station_ids), 2)

        station_1 = entity_mgr.stations[station_ids[0]]
        station_2 = entity_mgr.stations[station_ids[1]]
        env._full_backbone_cache = [
            BackboneVisit(node_id=station_1.station_id, arrival_time=60.0, departure_time=60.0 + 1e-6),
            BackboneVisit(node_id=station_2.station_id, arrival_time=120.0, departure_time=120.0 + 1e-6),
        ]

        self._inject_order_at(
            env,
            order_id="ORDER-P6-PLAN-01",
            position=station_1.location,
        )
        runtime_state = env.build_runtime_state_view()
        self.assertTrue(env._has_authorized_orders_now(runtime_state))
        plan_v0 = env._current_coarse_plan
        self.assertIsNotNone(plan_v0)
        self.assertGreaterEqual(plan_v0.plan_version, 0)
        self.assertIn(station_1.station_id, env._active_launch_stations)
        self.assertNotIn(station_2.station_id, env._active_launch_stations)

        env._require_order_manager().pending_orders.clear()
        self._inject_order_at(
            env,
            order_id="ORDER-P6-PLAN-02",
            position=station_2.location,
        )
        env._fallback_event_times[:] = [env._t_now, env._t_now]

        runtime_state = env.build_runtime_state_view()
        plan_v1 = env._refresh_coarse_plan_if_needed(runtime_state)
        self.assertGreater(plan_v1.plan_version, plan_v0.plan_version)
        self.assertIn(station_2.station_id, env._active_launch_stations)
        self.assertNotIn(station_1.station_id, env._active_launch_stations)

    def test_candidate_builder_keeps_masks_and_lookup_aligned(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station_ids = sorted(entity_mgr.stations)
        station_fast = entity_mgr.stations[station_ids[0]]
        station_safe = entity_mgr.stations[station_ids[1]]

        order = self._inject_order_at(
            env,
            order_id="ORDER-P6-CAND-01",
            position=Position3D(
                x=drone.current_loc.x + 10.0,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
        )
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_fast.station_id,
                arrival_time=t_deliver + 1.0,
                departure_time=t_deliver + 1.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=station_safe.station_id,
                arrival_time=t_deliver + 600.0,
                departure_time=t_deliver + 600.0 + 1e-6,
            ),
        ]

        runtime_state = env.build_runtime_state_view()
        coarse_plan = env._build_coarse_plan_view(env._t_now)
        candidate_out = env._candidate_builder.build(
            runtime_state=runtime_state,
            coarse_plan=coarse_plan,
            deciding_drone_id=drone_id,
            trigger_type="test_idle",
            trigger_station_id=None,
            last_seen_plan_version=-1,
        )

        self.assertEqual(candidate_out.root_branch_mask, (True, True))
        self.assertTrue(candidate_out.has_wait_action)
        self.assertEqual(candidate_out.candidate_features.uav_self.plan_version_delta, 1)
        self.assertTrue(candidate_out.order_mask[0])
        self.assertFalse(any(candidate_out.order_mask[1:]))
        self.assertEqual(sum(candidate_out.recovery_mask[0]), 1)
        self.assertTrue(candidate_out.mode_mask[0][0], "mode B 应保留")
        self.assertTrue(candidate_out.mode_mask[0][1], "mode C 应保留")

        resolved_wait = candidate_out.resolved_action_lookup.resolve(root_branch_idx=0)
        resolved_mode_c = candidate_out.resolved_action_lookup.resolve(
            root_branch_idx=1,
            order_idx=0,
            mode_idx=1,
            recovery_idx=0,
        )
        self.assertEqual(resolved_wait, WAIT_ACTION)
        self.assertEqual(resolved_mode_c.order_id, order.order_id)
        self.assertEqual(resolved_mode_c.mode, PolicyMode.C)
        self.assertEqual(resolved_mode_c.recover_node_id, station_safe.station_id)


if __name__ == "__main__":
    unittest.main()
