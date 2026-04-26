#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 5b env adapter tests.

运行方式：
  python -m unittest backend.training.test_env_adapter_phase5b
"""

from __future__ import annotations

import math
import unittest

from core.entities.order import Order
from core.entities.primitives import Position3D

from .contracts import PolicyMode
from .env_adapter import (
    BackboneVisit,
    DispatchAction,
    TrainingDroneState,
    TrainingEnvAdapter,
)


class TestTrainingEnvAdapterPhase5b(unittest.TestCase):
    def _make_env(self) -> TrainingEnvAdapter:
        return TrainingEnvAdapter()

    def _first_idle_drone_id(self, env: TrainingEnvAdapter) -> str:
        for drone_id, state in env._drone_state.items():
            if state == TrainingDroneState.IDLE:
                return drone_id
        self.fail("默认场景中至少应有一架 depot-home 无人机处于 idle")

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
        drone_id = self._first_idle_drone_id(env)
        env._enqueue_decision(drone_id, "test_idle", None)
        return env, drone_id

    def _inject_order(
        self,
        env: TrainingEnvAdapter,
        *,
        drone_id: str,
        order_id: str,
        offset_x: float = 10.0,
        payload_weight: float = 1.0,
    ) -> Order:
        drone = env._require_entity_manager().drones[drone_id]
        order = Order(
            order_id=order_id,
            create_time=env._t_now,
            deadline=env._t_now + 3600.0,
            delivery_loc=Position3D(
                x=drone.current_loc.x + offset_x,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
            payload_weight=payload_weight,
        )
        env._require_order_manager().pending_orders[order.order_id] = order
        return order

    def test_mode_c_action_mask_filters_by_timestamp_and_energy(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station_ids = sorted(entity_mgr.stations)
        self.assertGreaterEqual(len(station_ids), 2)

        order = self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5B-01")
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_ids[0],
                arrival_time=t_deliver + 1.0,
                departure_time=t_deliver + 1.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=station_ids[1],
                arrival_time=t_deliver + 600.0,
                departure_time=t_deliver + 600.0 + 1e-6,
            ),
        ]

        coarse_plan = env._build_coarse_plan_view(env._t_now)
        action_lookup = env._build_action_lookup(
            drone_id=drone_id,
            coarse_plan=coarse_plan,
        )
        mode_c_nodes = {
            action.recover_node_id
            for action in action_lookup
            if isinstance(action, DispatchAction) and action.mode == PolicyMode.C
        }

        self.assertNotIn(station_ids[0], mode_c_nodes)
        self.assertIn(station_ids[1], mode_c_nodes)

    def test_mode_b_return_host_uses_score_instead_of_depot_priority(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station = next(iter(entity_mgr.stations.values()))

        drone.current_loc = Position3D(
            x=station.location.x,
            y=station.location.y,
            z=station.location.z,
        )

        selected_host = env._select_return_host_mode_b(drone_id, current_time=env._t_now)

        self.assertIsNotNone(selected_host)
        self.assertEqual(selected_host.station_id, station.station_id)

    def test_post_delivery_revalidation_failure_enters_fallback(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        recover_node_id = sorted(entity_mgr.stations)[0]

        order = self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5B-02")
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=t_deliver + 600.0,
                departure_time=t_deliver + 600.0 + 1e-6,
            )
        ]

        trigger = env._decision_queue.pop(0)
        decision = env._build_decision_context(trigger)
        action = DispatchAction(
            order_id=order.order_id,
            mode=PolicyMode.C,
            recover_node_id=recover_node_id,
        )
        self.assertIn(action, decision.action_lookup)

        env._apply_dispatch_action(trigger, action)
        deliver_leg = env._flight_legs[drone_id]

        # 送达前把卡车到站时刻改成“已晚于当前复核窗口”，验证 5b 的时间复核会拒绝原节点。
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=max(0.0, deliver_leg.arrival_time - 1.0),
                departure_time=deliver_leg.arrival_time + 120.0,
            )
        ]

        reward = env._advance_to_event(deliver_leg.arrival_time)

        self.assertEqual(reward, env._cfg.R_delivery_bonus)
        self.assertEqual(
            env._drone_state[drone_id],
            TrainingDroneState.FALLBACK_RECOVERY,
        )
        self.assertIn(drone_id, env._fallback_leg)
        self.assertEqual(env._flight_legs[drone_id].kind, "fallback_recovery")

    def test_fallback_reward_accumulates_during_recovery(self) -> None:
        env, drone_id = self._reset_controlled_env()
        drone = env._require_entity_manager().drones[drone_id]
        depot = env._require_depot()

        drone.current_loc = Position3D(
            x=depot.location.x + 300.0,
            y=depot.location.y,
            z=depot.location.z,
        )
        env._drone_state[drone_id] = TrainingDroneState.DELIVERED
        env._planned_route_stop_i = len(env._planned_route_stops)

        entered = env._enter_fallback_recovery(drone_id)
        self.assertTrue(entered)

        leg = env._fallback_leg[drone_id]
        delta_t = min(5.0, max(1.0, (leg.arrival_time - env._t_now) / 2.0))

        reward = env._advance_to_event(env._t_now + delta_t)

        self.assertAlmostEqual(reward, -env._cfg.lambda_miss * delta_t, places=6)
        self.assertEqual(
            env._drone_state[drone_id],
            TrainingDroneState.FALLBACK_RECOVERY,
        )
        self.assertAlmostEqual(
            env._last_reward_breakdown["fallback"],
            -env._cfg.lambda_miss * delta_t,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
