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
    DecisionTrigger,
    DispatchAction,
    FALLBACK_CAUSE_NO_POST_DELIVERY_C_NODE,
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

    def _first_riding_drone_id(self, env: TrainingEnvAdapter) -> str:
        for drone_id, state in env._drone_state.items():
            if state == TrainingDroneState.RIDING_WITH_TRUCK:
                return drone_id
        self.fail("默认场景中至少应有一架 truck-home 无人机处于 riding_with_truck")

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

    def test_mode_c_dispatch_commit_records_planned_commitment_metrics(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        recover_node_id = sorted(entity_mgr.stations)[0]

        order = self._inject_order(
            env,
            drone_id=drone_id,
            order_id="ORDER-P5C-ETA-01",
            offset_x=3000.0,
        )
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=t_deliver + 900.0,
                departure_time=t_deliver + 900.0 + 1e-6,
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

        commit = env._dispatch_commit[drone_id]
        recover_host = env._resolve_fixed_node(recover_node_id)
        expected_delivery_finish = (
            env._estimate_delivery_arrival_time(drone, order)
            + env._scene_solver_params().drone_service_time_order_s
        )
        expected_uav_arrival_lb = expected_delivery_finish + env._estimate_flight_time(
            drone=drone,
            from_pos=order.delivery_loc,
            to_pos=recover_host.get_location(expected_delivery_finish),
        )
        expected_truck_arrival = t_deliver + 900.0

        self.assertAlmostEqual(
            commit.planned_truck_arrival_time,
            expected_truck_arrival,
            places=6,
        )
        self.assertAlmostEqual(
            commit.planned_uav_arrival_time_lb,
            expected_uav_arrival_lb,
            places=6,
        )
        self.assertAlmostEqual(
            commit.planned_execution_slack_sec,
            expected_truck_arrival - expected_uav_arrival_lb,
            places=6,
        )

    def test_mode_c_dispatch_without_node_selects_recovery_after_delivery_service(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station_id = sorted(entity_mgr.stations)[0]
        depot_id = env._require_depot().depot_id

        order = self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-POST-01")
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        t_service_finish = (
            t_deliver + env._scene_solver_params().drone_service_time_order_s
        )
        depot = env._require_depot()
        depot_uav_arrival = t_service_finish + env._estimate_flight_time(
            drone=drone,
            from_pos=order.delivery_loc,
            to_pos=depot.get_location(t_service_finish),
        )
        depot_truck_eta = (
            depot_uav_arrival + env._cfg.rendezvous_execution_margin_sec + 60.0
        )
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_id,
                arrival_time=t_service_finish + 1.0,
                departure_time=t_service_finish + 1.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=depot_id,
                arrival_time=depot_truck_eta,
                departure_time=depot_truck_eta + 1e-6,
            ),
        ]
        env._enqueue_decision(drone_id, "test_idle", None)

        trigger = env._decision_queue.pop(0)
        env._apply_dispatch_action(
            trigger,
            DispatchAction(order_id=order.order_id, mode=PolicyMode.C),
        )
        self.assertIsNone(env._dispatch_commit[drone_id].selected_recover_node)
        self.assertNotIn(drone_id, env._reservations)

        deliver_leg = env._flight_legs[drone_id]
        env._advance_to_event(deliver_leg.arrival_time)
        service_finish_time = env._delivery_service_legs[drone_id].finish_time
        reward_before_service = env._agent_cost_accum.get(drone_id, 0.0)
        env._advance_to_event(service_finish_time)

        commit = env._dispatch_commit[drone_id]
        self.assertEqual(commit.selected_recover_node, depot_id)
        self.assertAlmostEqual(commit.planned_truck_arrival_time, depot_truck_eta, places=6)
        self.assertIn(drone_id, env._reservations)
        self.assertEqual(env._reservations[drone_id].recover_node, depot_id)
        self.assertEqual(env._drone_state[drone_id], TrainingDroneState.RETURN_TO_RENDEZVOUS)
        self.assertEqual(env._flight_legs[drone_id].target_node_id, depot_id)
        self.assertAlmostEqual(
            env._agent_cost_accum.get(drone_id, 0.0) - reward_before_service,
            env._cfg.mode_c_attempt_bonus,
            places=6,
        )

    def test_mode_c_dispatch_without_post_delivery_node_enters_fallback(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station_id = sorted(entity_mgr.stations)[0]

        order = self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-POST-FAIL")
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        t_service_finish = (
            t_deliver + env._scene_solver_params().drone_service_time_order_s
        )
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_id,
                arrival_time=t_service_finish + 1.0,
                departure_time=t_service_finish + 1.0 + 1e-6,
            )
        ]
        env._enqueue_decision(drone_id, "test_idle", None)

        trigger = env._decision_queue.pop(0)
        env._apply_dispatch_action(
            trigger,
            DispatchAction(order_id=order.order_id, mode=PolicyMode.C),
        )

        deliver_leg = env._flight_legs[drone_id]
        env._advance_to_event(deliver_leg.arrival_time)
        service_finish_time = env._delivery_service_legs[drone_id].finish_time
        env._advance_to_event(service_finish_time)

        self.assertEqual(env._drone_state[drone_id], TrainingDroneState.FALLBACK_RECOVERY)
        self.assertNotIn(drone_id, env._reservations)
        self.assertIsNone(env._dispatch_commit[drone_id].selected_recover_node)
        self.assertEqual(env._episode_mode_c_post_delivery_revalidation_fail_count, 1)
        self.assertEqual(env._fallback_leg[drone_id].cause, FALLBACK_CAUSE_NO_POST_DELIVERY_C_NODE)
        self.assertEqual(
            env._episode_fallback_cause_counts[FALLBACK_CAUSE_NO_POST_DELIVERY_C_NODE],
            1,
        )
        self.assertEqual(env._episode_ppo_attributed_fallback_count, 1)

    def test_mode_c_riding_launch_commitment_uses_decision_time_truck_eta(self) -> None:
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

        drone_id = self._first_riding_drone_id(env)
        recover_node_id = sorted(env._require_entity_manager().stations)[0]
        order = self._inject_order(
            env,
            drone_id=drone_id,
            order_id="ORDER-P5C-ETA-TRUCK",
            offset_x=1000.0,
        )
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=env._t_now + 1200.0,
                departure_time=env._t_now + 1200.0 + 1e-6,
            )
        ]

        trigger = DecisionTrigger(
            decision_id=0,
            drone_id=drone_id,
            trigger_type="truck_station_arrival",
            trigger_station_id=recover_node_id,
        )
        env._apply_dispatch_action(
            trigger,
            DispatchAction(
                order_id=order.order_id,
                mode=PolicyMode.C,
                recover_node_id=recover_node_id,
            ),
        )

        commit = env._dispatch_commit[drone_id]

        self.assertAlmostEqual(
            float(commit.planned_truck_arrival_time),
            env._t_now + 1200.0,
            places=6,
        )
        self.assertIsNotNone(commit.planned_uav_arrival_time_lb)
        self.assertIsNotNone(commit.planned_execution_slack_sec)

    def test_runtime_state_reservation_view_does_not_expose_fixed_expiry(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        recover_node_id = sorted(entity_mgr.stations)[0]

        order = self._inject_order(
            env,
            drone_id=drone_id,
            order_id="ORDER-P5C-ETA-TRUCK-GUARD",
            offset_x=50.0,
        )
        t_arrive_truck = env._t_now + 1800.0
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=t_arrive_truck,
                departure_time=t_arrive_truck + 1e-6,
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
        self.assertFalse(hasattr(reservation, "expires_at"))

    def test_rendezvous_wait_timeout_switches_to_fallback_after_arrival(self) -> None:
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
            ),
            BackboneVisit(
                node_id=env._require_depot().depot_id,
                arrival_time=t_deliver + 1200.0,
                departure_time=t_deliver + 1200.0 + 1e-6,
            ),
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
        service_finish_time = env._delivery_service_legs[drone_id].finish_time
        env._advance_to_event(service_finish_time)

        rendezvous_leg = env._flight_legs[drone_id]
        env._advance_to_event(rendezvous_leg.arrival_time)

        coarse_plan = env._build_coarse_plan_view(env._t_now)
        t_arrive_truck = coarse_plan.truck_eta_map[recover_node_id]
        self.assertGreater(
            t_arrive_truck - rendezvous_leg.arrival_time,
            env._cfg.rendezvous_max_wait_sec,
        )
        timeout_probe_t = (
            rendezvous_leg.arrival_time
            + env._cfg.rendezvous_max_wait_sec
            + 1.0
        )
        reward = env._advance_to_event(timeout_probe_t)
        expected_wait_penalty = -env._cfg.lambda_wait * (
            timeout_probe_t - rendezvous_leg.arrival_time
        )

        self.assertAlmostEqual(
            reward,
            expected_wait_penalty - env._cfg.lambda_res_timeout * 1.0,
            places=6,
        )
        self.assertEqual(env._drone_state[drone_id], TrainingDroneState.FALLBACK_RECOVERY)
        self.assertNotIn(drone_id, env._reservations)
        self.assertEqual(env._flight_legs[drone_id].kind, "fallback_recovery")

    def test_return_to_rendezvous_does_not_timeout_until_arrival(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        recover_node_id = sorted(entity_mgr.stations)[0]

        order = self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-02B")
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=t_deliver + 600.0,
                departure_time=t_deliver + 600.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=env._require_depot().depot_id,
                arrival_time=t_deliver + 1200.0,
                departure_time=t_deliver + 1200.0 + 1e-6,
            ),
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
        service_finish_time = env._delivery_service_legs[drone_id].finish_time
        env._advance_to_event(service_finish_time)

        rendezvous_leg = env._flight_legs[drone_id]
        missed_truck_arrival = rendezvous_leg.arrival_time - 1.0
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=recover_node_id,
                arrival_time=missed_truck_arrival,
                departure_time=missed_truck_arrival + 1e-6,
            ),
        ]

        env._advance_to_event(missed_truck_arrival + 0.5)
        self.assertEqual(
            env._drone_state[drone_id],
            TrainingDroneState.RETURN_TO_RENDEZVOUS,
        )
        self.assertIn(drone_id, env._reservations)

        env._advance_to_event(rendezvous_leg.arrival_time)
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

        expected_opportunity_penalty = -env._cfg.wait_opportunity_penalty_coef * 0.1
        self.assertAlmostEqual(
            result.reward,
            -env._cfg.wait_idle_penalty_coef * env._cfg.max_wait_decision_gap_sec
            + expected_opportunity_penalty,
            places=6,
        )
        self.assertAlmostEqual(
            result.info["wait_opportunity_penalty"],
            expected_opportunity_penalty,
            places=6,
        )
        self.assertAlmostEqual(
            result.info["reward_breakdown"]["wait_opportunity"],
            expected_opportunity_penalty,
            places=6,
        )
        self.assertAlmostEqual(
            result.runtime_state.t_now,
            env._cfg.max_wait_decision_gap_sec,
            places=6,
        )
        self.assertEqual(result.info["wait_delta"], env._cfg.max_wait_decision_gap_sec)
        self.assertIsNotNone(result.decision_context)
        self.assertTrue(
            any(
                trigger.drone_id == drone_id and trigger.trigger_type == "wait_resume"
                for trigger in env._decision_queue
            )
        )

    def test_online_interfaces_apply_wait_without_implicit_advance(self) -> None:
        env, drone_id = self._reset_controlled_env()
        env._planned_route_stop_i = len(env._planned_route_stops)
        self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-ONLINE-WAIT")
        env._enqueue_decision(drone_id, "test_idle", None)

        self.assertAlmostEqual(env.peek_next_decision_time(), env._t_now, places=6)
        applied = env.apply_decision(WAIT_ACTION)

        self.assertAlmostEqual(applied.runtime_state.t_now, 0.0, places=6)
        self.assertIsNone(applied.decision_context)
        self.assertEqual(env._drone_state[drone_id], TrainingDroneState.ACTIVE_WAIT)
        self.assertAlmostEqual(
            env._active_wait_until[drone_id],
            env._cfg.max_wait_decision_gap_sec,
            places=6,
        )

        advanced = env.advance_to_time(env._cfg.upper_horizon_sec)

        self.assertAlmostEqual(
            advanced.runtime_state.t_now,
            env._cfg.max_wait_decision_gap_sec,
            places=6,
        )
        self.assertIsNotNone(advanced.decision_context)
        self.assertTrue(
            any(
                trigger.drone_id == drone_id and trigger.trigger_type == "wait_resume"
                for trigger in env._decision_queue
            )
        )

    def test_step_preserves_delayed_attribution_carry_in_before_current_window(self) -> None:
        env, drone_id = self._reset_controlled_env()
        env._planned_route_stop_i = len(env._planned_route_stops)
        self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-04-CARRY")
        env._enqueue_decision(drone_id, "test_idle", None)

        carried_reward = -7.5
        env._agent_cost_accum[drone_id] = carried_reward

        result = env.step(WAIT_ACTION)

        expected_opportunity_penalty = -env._cfg.wait_opportunity_penalty_coef * 0.1
        expected_post_action = (
            -env._cfg.wait_idle_penalty_coef * env._cfg.max_wait_decision_gap_sec
            + expected_opportunity_penalty
        )
        self.assertAlmostEqual(
            result.reward,
            carried_reward + expected_post_action,
            places=6,
        )
        self.assertAlmostEqual(
            result.info["reward_breakdown"]["attributed_carry_in"],
            carried_reward,
            places=6,
        )
        self.assertAlmostEqual(
            result.info["reward_breakdown"]["attributed_post_action"],
            expected_post_action,
            places=6,
        )
        self.assertAlmostEqual(
            result.info["reward_breakdown"]["attributed_total"],
            carried_reward + expected_post_action,
            places=6,
        )
        self.assertNotIn(drone_id, env._agent_cost_accum)

    def test_wait_opportunity_penalty_requires_legal_dispatch(self) -> None:
        env, drone_id = self._reset_controlled_env()
        env._planned_route_stop_i = len(env._planned_route_stops)
        self._inject_order(
            env,
            drone_id=drone_id,
            order_id="ORDER-P5C-WAIT-INFEASIBLE",
            offset_x=1_000_000.0,
        )
        env._decision_queue.append(
            DecisionTrigger(
                decision_id=env._next_decision_id,
                drone_id=drone_id,
                trigger_type="test_idle",
                trigger_station_id=None,
            )
        )
        env._next_decision_id += 1

        result = env.step(WAIT_ACTION)

        self.assertNotIn("wait_opportunity_penalty", result.info)
        self.assertAlmostEqual(
            result.reward,
            -env._cfg.wait_idle_penalty_coef * env._cfg.max_wait_decision_gap_sec,
            places=6,
        )

    def test_consume_terminal_agent_costs_clears_tail_accumulator(self) -> None:
        env, drone_id = self._reset_controlled_env()
        other_drone_id = self._second_drone_id(env, drone_id)
        env._agent_cost_accum[drone_id] = -3.5
        env._agent_cost_accum[other_drone_id] = 1.25
        env._t_now = env._cfg.upper_horizon_sec

        pending = env.consume_terminal_agent_costs()

        self.assertEqual(
            pending,
            {
                drone_id: -3.5,
                other_drone_id: 1.25,
            },
        )
        self.assertFalse(env._agent_cost_accum)

    def test_no_authorized_orders_does_not_enqueue_decision(self) -> None:
        env, _drone_id = self._reset_controlled_env()
        env._decision_queue.clear()

        env._enqueue_initial_idle_decisions()

        self.assertFalse(env._decision_queue)
        self.assertIsNone(env.current_decision_context)

    def test_wake_stranded_idle_drones_requeues_idle_uavs_after_new_order_arrives(self) -> None:
        env, drone_id = self._reset_controlled_env()
        sleeping_drone_id = self._second_drone_id(env, drone_id)
        env._decision_queue.clear()

        env._enqueue_initial_idle_decisions()
        self.assertFalse(env._decision_queue)

        env._drone_state[sleeping_drone_id] = TrainingDroneState.ACTIVE_WAIT
        env._active_wait_resume[sleeping_drone_id] = TrainingDroneState.IDLE
        self._inject_order(env, drone_id=drone_id, order_id="ORDER-P5C-WAKE-01")

        env._wake_stranded_idle_drones()

        queued_ids = [item.drone_id for item in env._decision_queue]
        queued_trigger_types = {item.trigger_type for item in env._decision_queue}
        self.assertIn(drone_id, queued_ids)
        self.assertNotIn(sleeping_drone_id, queued_ids)
        self.assertEqual(queued_trigger_types, {"order_arrival_wake"})
        self.assertIsNotNone(env.current_decision_context)

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

    def test_mode_a_background_completion_updates_stats_without_reward(self) -> None:
        env = self._make_env()
        result = env.reset()
        self.assertIn("system_context_stats", result.info)

        order_mgr = env._require_order_manager()
        order_mgr.pending_orders.clear()
        order_mgr.assigned_orders.clear()
        order_mgr.completed_orders.clear()
        order_mgr._next_order_time = math.inf
        order_mgr._scheduled_dynamic = []
        order_mgr._scheduled_dynamic_i = 0
        env._decision_queue.clear()

        background_stop = next(
            stop
            for stop in env._planned_route_stops
            if stop.node_type == "customer" and stop.order_id in env._background_mode_a_pending
        )

        reward = env._advance_to_event(background_stop.arrival_time)

        self.assertEqual(reward, 0.0)
        self.assertNotIn("delivery_bonus", env._last_reward_breakdown)

        stats = env.build_system_context_stats()
        self.assertGreaterEqual(stats["mode_a_background_order_count"], 1)
        self.assertEqual(stats["mode_a_background_completed_count"], 1)
        self.assertAlmostEqual(
            stats["mode_a_background_completion_time_sum"],
            background_stop.arrival_time,
            places=6,
        )
        self.assertEqual(len(stats["truck_background_order_completion_events"]), 1)
        self.assertEqual(
            stats["truck_background_order_completion_events"][0]["order_id"],
            background_stop.order_id,
        )


if __name__ == "__main__":
    unittest.main()
