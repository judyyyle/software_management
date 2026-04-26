#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 5c env adapter tests.

运行方式：
  python -m unittest backend.training.test_env_adapter_phase5c
"""

from __future__ import annotations

import math
import unittest

from core.entities.order import Order
from core.entities.primitives import Position3D, TaskStatus

from .contracts import PolicyMode
from .env_adapter import (
    BackboneVisit,
    DispatchAction,
    ReservationState,
    TrainingDroneState,
    TrainingEnvAdapter,
    WAIT_ACTION,
)


class TestTrainingEnvAdapterPhase5c(unittest.TestCase):
    def _make_env(self) -> TrainingEnvAdapter:
        return TrainingEnvAdapter()

    def _first_idle_drone_id(self, env: TrainingEnvAdapter) -> str:
        for drone_id, state in env._drone_state.items():
            if state == TrainingDroneState.IDLE:
                return drone_id
        self.fail("默认场景中至少应有一架 depot-home 无人机处于 idle")

    def _second_drone_id(self, env: TrainingEnvAdapter, exclude: str) -> str:
        for drone_id in env._drone_state:
            if drone_id != exclude:
                return drone_id
        self.fail("默认场景中至少应有两架无人机")

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

    def _inject_order(
        self,
        env: TrainingEnvAdapter,
        *,
        drone_id: str,
        order_id: str,
        offset_x: float = 10.0,
        create_time: float | None = None,
        deadline: float | None = None,
        payload_weight: float = 1.0,
    ) -> Order:
        drone = env._require_entity_manager().drones[drone_id]
        spawn_time = env._t_now if create_time is None else create_time
        due_time = env._t_now + 3600.0 if deadline is None else deadline
        order = Order(
            order_id=order_id,
            create_time=spawn_time,
            deadline=due_time,
            delivery_loc=Position3D(
                x=drone.current_loc.x + offset_x,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
            payload_weight=payload_weight,
        )
        env._require_order_manager().pending_orders[order.order_id] = order
        return order

    def test_runtime_state_exposes_reservation_and_count(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        recover_node_id = sorted(entity_mgr.stations)[0]

        order = self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-01")
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=t_deliver + 600.0,
                departure_time=t_deliver + 600.0 + 1e-6,
            )
        ]
        env._enqueue_decision(drone_id, "test_idle", None)

        trigger = env._decision_queue.pop(0)
        env._apply_dispatch_action(
            trigger,
            DispatchAction(
                order_id=order.order_id,
                mode=PolicyMode.C,
                recover_node_id=recover_node_id,
            ),
        )

        runtime_state = env.build_runtime_state_view()
        reservation = runtime_state.drone_states[drone_id].reservation
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.recover_node, recover_node_id)
        self.assertIn(recover_node_id, runtime_state.reservation_count)
        self.assertEqual(runtime_state.reservation_count[recover_node_id], 1)

    def test_reservation_timeout_switches_to_fallback_after_delivery(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        recover_node_id = sorted(entity_mgr.stations)[0]

        order = self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-02")
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=t_deliver + 600.0,
                departure_time=t_deliver + 600.0 + 1e-6,
            )
        ]
        env._enqueue_decision(drone_id, "test_idle", None)

        trigger = env._decision_queue.pop(0)
        env._apply_dispatch_action(
            trigger,
            DispatchAction(
                order_id=order.order_id,
                mode=PolicyMode.C,
                recover_node_id=recover_node_id,
            ),
        )
        deliver_leg = env._flight_legs[drone_id]
        env._advance_to_event(deliver_leg.arrival_time)

        reservation = env._reservations[drone_id]
        env._reservations[drone_id] = ReservationState(
            recover_node=reservation.recover_node,
            issued_at=reservation.issued_at,
            expires_at=env._t_now + 0.25,
        )

        reward = env._advance_to_event(env._t_now + 1.0)

        self.assertAlmostEqual(
            reward,
            -env._cfg.lambda_res_timeout * 0.75,
            places=6,
        )
        self.assertEqual(env._drone_state[drone_id], TrainingDroneState.FALLBACK_RECOVERY)
        self.assertNotIn(drone_id, env._reservations)
        self.assertEqual(env._flight_legs[drone_id].kind, "fallback_recovery")

    def test_hard_overdue_penalty_removes_pending_order(self) -> None:
        env, drone_id = self._reset_controlled_env()
        env._planned_route_stop_i = len(env._planned_route_stops)

        order = self._inject_order(
            env,
            drone_id=drone_id,
            order_id="ORDER-P5C-03",
            create_time=-2000.0,
            deadline=-1000.0,
        )

        reward = env._advance_to_event(env._t_now + 1.0)

        self.assertAlmostEqual(
            reward,
            -env._cfg.lambda_overdue * 1.0 - env._cfg.hard_overdue_penalty_sec,
            places=6,
        )
        self.assertNotIn(order.order_id, env._require_order_manager().pending_orders)
        removed = next(item for item in env._require_order_manager().completed_orders if item.order_id == order.order_id)
        self.assertEqual(removed.status, TaskStatus.TIMEOUT)

    def test_wait_action_uses_exact_idle_penalty_and_gap_cap(self) -> None:
        env, drone_id = self._reset_controlled_env()
        env._planned_route_stop_i = len(env._planned_route_stops)
        self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-04")
        env._enqueue_decision(drone_id, "test_idle", None)

        result = env.step(WAIT_ACTION)

        self.assertAlmostEqual(
            result.reward,
            -env._cfg.wait_idle_penalty_coef * env._cfg.max_wait_decision_gap_sec,
            places=6,
        )
        self.assertAlmostEqual(
            result.runtime_state.t_now,
            env._cfg.max_wait_decision_gap_sec,
            places=6,
        )
        self.assertEqual(result.info["wait_delta"], env._cfg.max_wait_decision_gap_sec)
        self.assertIsNotNone(result.decision_context)
        self.assertEqual(result.decision_context.deciding_drone_id, drone_id)

    def test_no_authorized_orders_does_not_enqueue_decision(self) -> None:
        env, _drone_id = self._reset_controlled_env()
        env._decision_queue.clear()

        env._enqueue_initial_idle_decisions()

        self.assertFalse(env._decision_queue)
        self.assertIsNone(env.current_decision_context)

    def test_settle_per_dt_rewards_separates_wait_and_queue(self) -> None:
        env, drone_id = self._reset_controlled_env()
        other_drone_id = self._second_drone_id(env, drone_id)

        env._drone_state[drone_id] = TrainingDroneState.WAITING_FOR_TRUCK
        env._drone_state[other_drone_id] = TrainingDroneState.QUEUEING_AT_HOST

        reward, breakdown = env._settle_per_dt_rewards(t_prev=0.0, t_next=5.0)

        self.assertAlmostEqual(
            reward,
            -env._cfg.lambda_wait * 5.0 - env._cfg.lambda_queue * 5.0,
            places=6,
        )
        self.assertAlmostEqual(breakdown["wait"], -env._cfg.lambda_wait * 5.0, places=6)
        self.assertAlmostEqual(breakdown["queue"], -env._cfg.lambda_queue * 5.0, places=6)


if __name__ == "__main__":
    unittest.main()
