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
from dataclasses import replace

from core.entities.order import Order
from core.entities.primitives import Position3D

from .contracts import (
    PlannerTriggerContext,
    PolicyMode,
    ReservationConstraintState,
    ReservationPlanStatus,
    RouteDriftRef,
    TruckReservationConstraint,
)
from .env_adapter import (
    BackboneVisit,
    DispatchCommit,
    PlannedStop,
    ReservationState,
    TrainingDroneState,
    TrainingEnvAdapter,
    WAIT_ACTION,
)
from .order_source_adapter import OrderSourceMode, build_order_source
from .scene_loader import load_default_scene


class TestPhase6Integration(unittest.TestCase):
    def _make_env(self) -> TrainingEnvAdapter:
        return TrainingEnvAdapter()

    def _first_idle_drone_id(self, env: TrainingEnvAdapter) -> str:
        for drone_id, state in env._drone_state.items():
            if state == TrainingDroneState.IDLE:
                return drone_id
        self.fail("默认场景中至少应有一架 idle 无人机")

    def _first_riding_drone_id(self, env: TrainingEnvAdapter) -> str:
        for drone_id, state in env._drone_state.items():
            if state == TrainingDroneState.RIDING_WITH_TRUCK:
                return drone_id
        self.fail("默认场景中至少应有一架 riding_with_truck 无人机")

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

    def test_poisson_truck_only_dynamic_orders_are_not_uav_authorized(self) -> None:
        scene_ctx = load_default_scene()
        order_source = build_order_source(
            scene_ctx,
            mode=OrderSourceMode.POISSON,
            overrides={
                "poisson_arrival_rate": 0.0,
                "truck_only_dynamic_enabled": True,
                "truck_only_dynamic_orders_per_episode": 1,
                "truck_only_dynamic_spawn_start_s": 0.0,
                "truck_only_dynamic_spawn_end_s": 0.0,
                "truck_only_dynamic_weight_min_kg": 10.5,
                "truck_only_dynamic_weight_max_kg": 10.5,
                "truck_only_dynamic_deadline_window_min_min": 90,
                "truck_only_dynamic_deadline_window_max_min": 90,
            },
        )
        truck_only_entries = [
            entry
            for entry in order_source.scheduled_dynamic_orders
            if str(entry["order_id"]).startswith("TRUCKONLY-")
        ]
        self.assertEqual(len(truck_only_entries), 1)
        self.assertGreater(float(truck_only_entries[0]["payload_weight"]), 10.0)

        env = TrainingEnvAdapter(scene_ctx=scene_ctx, order_source=order_source)
        env.reset()
        order_mgr = env._require_order_manager()
        truck_only_order_id = str(truck_only_entries[0]["order_id"])
        if truck_only_order_id in order_mgr.pending_orders:
            coarse_plan = env._refresh_coarse_plan_if_needed(
                env.build_runtime_state_view()
            )
            self.assertFalse(coarse_plan.is_order_authorized(truck_only_order_id))
            self.assertNotIn(truck_only_order_id, coarse_plan.policy_mode_mask)
        else:
            completed_ids = {order.order_id for order in order_mgr.completed_orders}
            self.assertIn(truck_only_order_id, completed_ids)
            stats = env.build_system_context_stats()
            truck_only_events = [
                event
                for event in stats["truck_background_order_completion_events"]
                if event.get("truck_only_dynamic")
            ]
            self.assertEqual(len(truck_only_events), 1)
            self.assertEqual(truck_only_events[0]["order_id"], truck_only_order_id)

    def test_manual_truck_only_order_is_not_uav_authorized(self) -> None:
        env, drone_id = self._reset_controlled_env()
        drone = env._require_entity_manager().drones[drone_id]
        order = self._inject_order_at(
            env,
            order_id="TRUCKONLY-MANUAL-P6",
            position=Position3D(
                x=drone.current_loc.x + 100.0,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
            payload_weight=env._heavy_payload_capacity + 1.0,
        )

        coarse_plan = env._refresh_coarse_plan_if_needed(env.build_runtime_state_view())

        self.assertFalse(coarse_plan.is_order_authorized(order.order_id))
        self.assertNotIn(order.order_id, coarse_plan.policy_mode_mask)
        self.assertTrue(
            any(
                stop.node_type == "customer" and stop.order_id == order.order_id
                for stop in coarse_plan.truck_plan_stops
            )
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

    def test_planner_bridge_recovery_pool_exposes_future_backbone_nodes(self) -> None:
        env, _drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        station_ids = sorted(entity_mgr.stations)
        self.assertGreaterEqual(len(station_ids), 5)

        preferred_station_id = station_ids[4]
        preferred_station = entity_mgr.stations[preferred_station_id]
        order = self._inject_order_at(
            env,
            order_id="ORDER-P6-PLAN-RECOVERY-01",
            position=preferred_station.location,
        )

        for station_id in station_ids[:4]:
            station = entity_mgr.stations[station_id]
            station.serving_drones = {
                f"busy-{station_id}-{idx}": env._t_now + 600.0 + idx
                for idx in range(station.parking_slots)
            }
            station.wait_queue = [f"wait-{station_id}"]

        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_id,
                arrival_time=env._t_now + 60.0 * (idx + 1),
                departure_time=env._t_now + 60.0 * (idx + 1) + 1e-6,
            )
            for idx, station_id in enumerate(station_ids[:5])
        ]

        runtime_state = env.build_runtime_state_view()
        env._planner_bridge.reset_episode()
        coarse_plan = env._planner_bridge.maybe_replan(
            runtime_state,
            PlannerTriggerContext(
                t_now=env._t_now,
                backlog_new_orders=0,
                fallback_count_in_window=0,
                hard_failure_count_in_window=0,
                route_drift_ratio=0.0,
            ),
        )
        recovery_nodes = coarse_plan.recovery_pool[order.order_id]

        self.assertEqual(recovery_nodes, tuple(station_ids[:5]))
        self.assertIn(
            preferred_station_id,
            recovery_nodes,
            "PlannerBridge 应与 env 内联 coarse plan 一致，暴露卡车未来会经过的固定节点",
        )

    def test_poisson_planner_bridge_rejects_empty_backbone(self) -> None:
        env, _drone_id = self._reset_controlled_env()
        env._full_backbone_cache = []
        runtime_state = env.build_runtime_state_view()
        env._planner_bridge.reset_episode()

        with self.assertRaises(ValueError):
            env._planner_bridge.maybe_replan(
                runtime_state,
                PlannerTriggerContext(
                    t_now=env._t_now,
                    backlog_new_orders=0,
                    fallback_count_in_window=0,
                    hard_failure_count_in_window=0,
                    route_drift_ratio=0.0,
                ),
            )

    def test_benchmark_planner_bridge_allows_empty_backbone(self) -> None:
        scene_ctx = load_default_scene()
        benchmark_source = build_order_source(
            scene_ctx,
            mode=OrderSourceMode.BENCHMARK,
        )
        env = TrainingEnvAdapter(
            scene_ctx=scene_ctx,
            order_source=benchmark_source,
        )
        env.reset()
        env._full_backbone_cache = []
        runtime_state = env.build_runtime_state_view()
        env._planner_bridge.reset_episode()

        coarse_plan = env._planner_bridge.maybe_replan(
            runtime_state,
            PlannerTriggerContext(
                t_now=env._t_now,
                backlog_new_orders=0,
                fallback_count_in_window=0,
                hard_failure_count_in_window=0,
                route_drift_ratio=0.0,
            ),
        )
        self.assertTrue(coarse_plan.allow_empty_backbone_route)
        self.assertEqual(coarse_plan.truck_backbone_route, ())

    def test_planner_bridge_reports_reservation_outcomes(self) -> None:
        env, _drone_id = self._reset_controlled_env()
        station_ids = sorted(env._require_entity_manager().stations)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_ids[0],
                arrival_time=100.0,
                departure_time=100.0,
            ),
            BackboneVisit(
                node_id=station_ids[1],
                arrival_time=230.0,
                departure_time=230.0,
            ),
        ]
        runtime_state = env.build_runtime_state_view()
        env._planner_bridge.reset_episode()
        entity_mgr = env._require_entity_manager()
        fixed_service = max(
            env._scene_solver_params().truck_drone_launch_time_s,
            env._scene_solver_params().truck_drone_recover_time_s,
        )
        station_03_eta = env._estimate_truck_road_travel_time(
            runtime_state.truck_current_loc,
            entity_mgr.stations[station_ids[2]].location,
        )
        station_02_eta = (
            station_03_eta
            + fixed_service
            + env._estimate_truck_road_travel_time(
                entity_mgr.stations[station_ids[2]].location,
                entity_mgr.stations[station_ids[1]].location,
            )
        )

        coarse_plan = env._planner_bridge.maybe_replan(
            runtime_state,
            PlannerTriggerContext(
                t_now=0.0,
                backlog_new_orders=0,
                fallback_count_in_window=0,
                hard_failure_count_in_window=0,
                route_drift_ratio=0.0,
            ),
            reservation_constraints=(
                TruckReservationConstraint(
                    reservation_id="res-kept",
                    drone_id="drone-kept",
                    node_id=station_ids[2],
                    state=ReservationConstraintState.HARD,
                    eta_ref=station_03_eta,
                    max_eta_drift_sec=60.0,
                    issued_at=0.0,
                    related_order_id="order-kept",
                ),
                TruckReservationConstraint(
                    reservation_id="res-drifted",
                    drone_id="drone-drifted",
                    node_id=station_ids[1],
                    state=ReservationConstraintState.HARD,
                    eta_ref=station_02_eta - 30.0,
                    max_eta_drift_sec=60.0,
                    issued_at=0.0,
                    related_order_id="order-drifted",
                ),
                TruckReservationConstraint(
                    reservation_id="res-invalidated",
                    drone_id="drone-invalidated",
                    node_id=station_ids[0],
                    state=ReservationConstraintState.STRONG_HARD,
                    eta_ref=100.0,
                    max_eta_drift_sec=60.0,
                    issued_at=0.0,
                    related_order_id="order-invalidated",
                ),
            ),
        )

        outcomes = coarse_plan.reservation_outcomes
        self.assertEqual(outcomes["res-kept"].status, ReservationPlanStatus.KEPT)
        self.assertEqual(
            outcomes["res-drifted"].status,
            ReservationPlanStatus.DRIFTED,
        )
        self.assertEqual(outcomes["res-drifted"].eta_drift_sec, 30.0)
        self.assertEqual(
            outcomes["res-invalidated"].status,
            ReservationPlanStatus.INVALIDATED,
        )
        self.assertEqual(
            outcomes["res-invalidated"].invalidate_cause,
            "eta_late_exceeds_threshold",
        )

    def test_env_adapter_builds_post_delivery_reservation_constraints(self) -> None:
        env, drone_id = self._reset_controlled_env()
        station_id = sorted(env._require_entity_manager().stations)[0]
        env._reservations[drone_id] = ReservationState(
            recover_node=station_id,
            issued_at=12.0,
        )
        env._reservation_count[station_id] = 1
        env._dispatch_commit[drone_id] = DispatchCommit(
            order_id="ORDER-C-RES-CONSTRAINT",
            mode=PolicyMode.C,
            selected_recover_node=station_id,
            trigger_station_id=None,
            planned_truck_arrival_time=180.0,
        )

        runtime_state = env.build_runtime_state_view()
        env._drone_state[drone_id] = TrainingDroneState.FLYING_TO_DELIVER
        self.assertEqual(env._build_truck_reservation_constraints(runtime_state), ())

        env._drone_state[drone_id] = TrainingDroneState.WAITING_FOR_TRUCK
        constraints = env._build_truck_reservation_constraints(runtime_state)

        self.assertEqual(len(constraints), 1)
        self.assertEqual(
            constraints[0].state,
            ReservationConstraintState.STRONG_HARD,
        )
        self.assertEqual(constraints[0].node_id, station_id)
        self.assertEqual(constraints[0].eta_ref, 180.0)
        self.assertEqual(
            constraints[0].related_order_id,
            "ORDER-C-RES-CONSTRAINT",
        )

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
        t_deliver_finish = t_deliver + env._scene_solver_params().drone_service_time_order_s
        safe_recover_fly = env._estimate_flight_time(
            drone=drone,
            from_pos=order.delivery_loc,
            to_pos=station_safe.location,
        )
        t_safe_truck = (
            t_deliver_finish
            + safe_recover_fly
            + env._cfg.rendezvous_execution_margin_sec
            + 30.0
        )
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_fast.station_id,
                arrival_time=t_deliver + 1.0,
                departure_time=t_deliver + 1.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=station_safe.station_id,
                arrival_time=t_safe_truck,
                departure_time=t_safe_truck + 1e-6,
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
        self.assertTrue(candidate_out.mode_mask[0][0], "mode B 应保留")
        self.assertTrue(candidate_out.mode_mask[0][1], "mode C 应保留")
        order_feature = candidate_out.candidate_features.order_features[0]
        recovery_feature = candidate_out.candidate_features.recovery_features[0][0]
        self.assertTrue(order_feature.has_mode_c_action)
        self.assertAlmostEqual(
            order_feature.best_mode_c_rendezvous_margin,
            recovery_feature.rendezvous_margin,
        )
        self.assertEqual(
            order_feature.best_mode_c_node_type,
            recovery_feature.recover_node_type,
        )
        self.assertAlmostEqual(
            order_feature.best_mode_c_truck_eta_remaining,
            recovery_feature.truck_eta - runtime_state.t_now,
        )

        resolved_wait = candidate_out.resolved_action_lookup.resolve(root_branch_idx=0)
        resolved_mode_c = candidate_out.resolved_action_lookup.resolve(
            root_branch_idx=1,
            order_idx=0,
            mode_idx=1,
        )
        self.assertEqual(resolved_wait, WAIT_ACTION)
        self.assertEqual(resolved_mode_c.order_id, order.order_id)
        self.assertEqual(resolved_mode_c.mode, PolicyMode.C)
        self.assertIsNone(resolved_mode_c.recover_node_id)

    def test_public_candidate_output_entry_rebuilds_from_decision_context(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station_ids = sorted(entity_mgr.stations)
        station_fast = entity_mgr.stations[station_ids[0]]
        station_safe = entity_mgr.stations[station_ids[1]]

        order = self._inject_order_at(
            env,
            order_id="ORDER-P6-PUBLIC-01",
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

        env._enqueue_initial_idle_decisions()
        decision_context = env.current_decision_context
        self.assertIsNotNone(decision_context)

        candidate_out = env.build_candidate_output(
            decision_context,
            last_seen_plan_version=-1,
        )

        self.assertEqual(
            candidate_out.resolved_action_lookup.as_action_lookup(),
            decision_context.action_lookup,
        )
        self.assertEqual(
            candidate_out.candidate_features.order_features[0].order_id,
            order.order_id,
        )
        self.assertEqual(
            candidate_out.candidate_features.uav_self.plan_version_delta,
            decision_context.coarse_plan.plan_version + 1,
        )

    def test_riding_with_truck_alias_uses_runtime_recovery_pool(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station_ids = sorted(entity_mgr.stations)
        station_1 = entity_mgr.stations[station_ids[0]]
        station_2 = entity_mgr.stations[station_ids[1]]

        order = self._inject_order_at(
            env,
            order_id="ORDER-P6-RIDE-ALIAS-01",
            position=Position3D(
                x=station_2.location.x + 10.0,
                y=station_2.location.y,
                z=station_2.location.z,
            ),
        )

        runtime_state = env.build_runtime_state_view()
        coarse_plan = env._build_coarse_plan_view(env._t_now)
        custom_plan = replace(
            coarse_plan,
            truck_backbone_route=(station_1.station_id, station_2.station_id),
            truck_eta_map={
                station_1.station_id: runtime_state.t_now + 450.0,
                station_2.station_id: runtime_state.t_now + 260.0,
            },
            route_drift_ref={
                station_1.station_id: RouteDriftRef(
                    eta_ref=runtime_state.t_now + 450.0,
                    route_index_ref=0,
                ),
                station_2.station_id: RouteDriftRef(
                    eta_ref=runtime_state.t_now + 260.0,
                    route_index_ref=1,
                ),
            },
            recovery_pool={
                **coarse_plan.recovery_pool,
                order.order_id: (station_1.station_id, station_2.station_id),
            },
            launch_candidate_stations=(station_1.station_id, station_2.station_id),
        )

        candidate_out_alias = env._candidate_builder.build(
            runtime_state=runtime_state,
            coarse_plan=custom_plan,
            deciding_drone_id=drone_id,
            trigger_type="riding_with_truck",
            trigger_station_id=station_2.station_id,
            last_seen_plan_version=custom_plan.plan_version,
        )
        candidate_out_internal = env._candidate_builder.build(
            runtime_state=runtime_state,
            coarse_plan=custom_plan,
            deciding_drone_id=drone_id,
            trigger_type="truck_station_arrival",
            trigger_station_id=station_2.station_id,
            last_seen_plan_version=custom_plan.plan_version,
        )
        candidate_out_idle = env._candidate_builder.build(
            runtime_state=runtime_state,
            coarse_plan=custom_plan,
            deciding_drone_id=drone_id,
            trigger_type="test_idle",
            trigger_station_id=None,
            last_seen_plan_version=custom_plan.plan_version,
        )

        alias_recovery_nodes = [
            feature.recover_node_id
            for feature in candidate_out_alias.candidate_features.recovery_features[0]
            if feature.is_valid
        ]
        internal_recovery_nodes = [
            feature.recover_node_id
            for feature in candidate_out_internal.candidate_features.recovery_features[0]
            if feature.is_valid
        ]
        idle_recovery_nodes = [
            feature.recover_node_id
            for feature in candidate_out_idle.candidate_features.recovery_features[0]
            if feature.is_valid
        ]

        self.assertEqual(alias_recovery_nodes, [station_2.station_id])
        self.assertEqual(internal_recovery_nodes, [station_2.station_id])
        self.assertEqual(
            set(idle_recovery_nodes),
            {station_1.station_id, station_2.station_id},
        )

    def test_riding_with_truck_trigger_requires_station_id(self) -> None:
        env, drone_id = self._reset_controlled_env()
        drone = env._require_entity_manager().drones[drone_id]
        self._inject_order_at(
            env,
            order_id="ORDER-P6-RIDE-REQ-01",
            position=Position3D(
                x=drone.current_loc.x + 10.0,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
        )
        runtime_state = env.build_runtime_state_view()
        coarse_plan = env._build_coarse_plan_view(env._t_now)

        with self.assertRaises(ValueError):
            env._candidate_builder.build(
                runtime_state=runtime_state,
                coarse_plan=coarse_plan,
                deciding_drone_id=drone_id,
                trigger_type="riding_with_truck",
                trigger_station_id=None,
                last_seen_plan_version=coarse_plan.plan_version,
            )

    def test_candidate_builder_respects_policy_mode_mask(self) -> None:
        env, drone_id = self._reset_controlled_env()
        entity_mgr = env._require_entity_manager()
        drone = entity_mgr.drones[drone_id]
        station_ids = sorted(entity_mgr.stations)
        station_1 = entity_mgr.stations[station_ids[0]]
        station_2 = entity_mgr.stations[station_ids[1]]

        order = self._inject_order_at(
            env,
            order_id="ORDER-P6-POLICY-01",
            position=Position3D(
                x=drone.current_loc.x + 10.0,
                y=drone.current_loc.y,
                z=drone.current_loc.z,
            ),
        )
        t_deliver = env._estimate_delivery_arrival_time(drone, order)
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_1.station_id,
                arrival_time=t_deliver + 300.0,
                departure_time=t_deliver + 300.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=station_2.station_id,
                arrival_time=t_deliver + 600.0,
                departure_time=t_deliver + 600.0 + 1e-6,
            ),
        ]

        runtime_state = env.build_runtime_state_view()
        coarse_plan = env._build_coarse_plan_view(env._t_now)
        restricted_plan = replace(
            coarse_plan,
            policy_mode_mask={
                **coarse_plan.policy_mode_mask,
                order.order_id: frozenset({PolicyMode.B}),
            },
        )
        candidate_out = env._candidate_builder.build(
            runtime_state=runtime_state,
            coarse_plan=restricted_plan,
            deciding_drone_id=drone_id,
            trigger_type="test_idle",
            trigger_station_id=None,
            last_seen_plan_version=restricted_plan.plan_version,
        )

        self.assertTrue(candidate_out.order_mask[0])
        self.assertEqual(candidate_out.mode_mask[0], (True, False))
        self.assertEqual(
            candidate_out.candidate_features.order_features[0].order_id,
            order.order_id,
        )
        self.assertTrue(
            candidate_out.candidate_features.order_features[0].has_mode_b_action
        )
        with self.assertRaises(KeyError):
            candidate_out.resolved_action_lookup.resolve(
                root_branch_idx=1,
                order_idx=0,
                mode_idx=1,
            )

    def test_riding_wait_tracks_actual_elapsed_until_trigger_station(self) -> None:
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
        env._planner_bridge = None
        env._current_coarse_plan = None

        drone_id = self._first_riding_drone_id(env)
        entity_mgr = env._require_entity_manager()
        truck = env._require_truck()
        depot = env._require_depot()
        station_ids = sorted(entity_mgr.stations)
        self.assertGreaterEqual(len(station_ids), 2)
        station_1 = entity_mgr.stations[station_ids[0]]
        station_2 = entity_mgr.stations[station_ids[1]]

        env._planned_route_stops = [
            PlannedStop(
                seq=0,
                node_type="depot",
                node_id=depot.depot_id,
                position=Position3D(
                    x=truck.current_loc.x,
                    y=truck.current_loc.y,
                    z=truck.current_loc.z,
                ),
                order_id=None,
                arrival_time=0.0,
                departure_time=1e-6,
            ),
            PlannedStop(
                seq=1,
                node_type="station",
                node_id=station_1.station_id,
                position=station_1.location,
                order_id=None,
                arrival_time=60.0,
                departure_time=60.0 + 1e-6,
            ),
            PlannedStop(
                seq=2,
                node_type="station",
                node_id=station_2.station_id,
                position=station_2.location,
                order_id=None,
                arrival_time=120.0,
                departure_time=120.0 + 1e-6,
            ),
        ]
        env._planned_route_stop_i = 1
        env._full_backbone_cache = [
            BackboneVisit(
                node_id=station_1.station_id,
                arrival_time=60.0,
                departure_time=60.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=station_2.station_id,
                arrival_time=120.0,
                departure_time=120.0 + 1e-6,
            ),
            BackboneVisit(
                node_id=depot.depot_id,
                arrival_time=180.0,
                departure_time=180.0 + 1e-6,
            ),
        ]

        self._inject_order_at(
            env,
            order_id="ORDER-P6-RIDE-WAIT-01",
            position=Position3D(
                x=truck.current_loc.x + 20.0,
                y=truck.current_loc.y,
                z=truck.current_loc.z,
            ),
            deadline_offset=7200.0,
        )
        for other_drone_id, state in list(env._drone_state.items()):
            if other_drone_id == drone_id:
                continue
            if state == TrainingDroneState.IDLE:
                env._drone_state[other_drone_id] = TrainingDroneState.ACTIVE_WAIT
                env._active_wait_resume[other_drone_id] = TrainingDroneState.IDLE

        env._active_launch_stations = {station_2.station_id}
        env._enqueue_decision(
            drone_id,
            trigger_type="truck_station_arrival",
            trigger_station_id=station_1.station_id,
        )
        self.assertTrue(env._decision_queue)

        t_start = env._t_now
        result = env.step(WAIT_ACTION)
        elapsed = env._t_now - t_start
        expected_elapsed = 120.0 - t_start

        self.assertAlmostEqual(elapsed, expected_elapsed, places=6)
        self.assertEqual(
            result.info["wait_mode"],
            "deferred_riding_with_truck",
        )
        self.assertNotIn("wait_delta", result.info)
        self.assertAlmostEqual(
            result.reward,
            -env._cfg.wait_idle_penalty_coef * expected_elapsed,
            places=6,
        )
        self.assertIn(
            env._drone_state[drone_id],
            {TrainingDroneState.RIDING_WITH_TRUCK, TrainingDroneState.ACTIVE_WAIT},
        )
        self.assertTrue(
            any(
                trigger.drone_id == drone_id
                and trigger.trigger_type == "truck_station_arrival"
                and trigger.trigger_station_id == station_2.station_id
                for trigger in env._decision_queue
            )
        )

    def test_candidate_builder_riding_trigger_applies_launch_delay_and_delivery_service(self) -> None:
        env = self._make_env()
        env.reset()

        drone_id = self._first_riding_drone_id(env)
        entity_mgr = env._require_entity_manager()
        station_ids = sorted(entity_mgr.stations)
        launch_station = entity_mgr.stations[station_ids[0]]
        recover_station = entity_mgr.stations[station_ids[1]]
        order = self._inject_order_at(
            env,
            order_id="ORDER-P6-LAUNCH-SVC-01",
            position=Position3D(
                x=launch_station.location.x + 10.0,
                y=launch_station.location.y,
                z=launch_station.location.z,
            ),
        )

        runtime_state = env.build_runtime_state_view()
        launch_time = (
            runtime_state.t_now + env._scene_solver_params().truck_drone_launch_time_s
        )
        deliver_fly = env._estimate_flight_time(
            drone=entity_mgr.drones[drone_id],
            from_pos=launch_station.location,
            to_pos=order.delivery_loc,
        )
        recover_fly = env._estimate_flight_time(
            drone=entity_mgr.drones[drone_id],
            from_pos=order.delivery_loc,
            to_pos=recover_station.location,
        )
        truck_eta = (
            launch_time
            + deliver_fly
            + recover_fly
            + env._cfg.rendezvous_execution_margin_sec
            + env._scene_solver_params().drone_service_time_order_s
            - 1.0
        )
        coarse_plan = env._build_coarse_plan_view(env._t_now)
        custom_plan = replace(
            coarse_plan,
            truck_backbone_route=(launch_station.station_id, recover_station.station_id),
            truck_eta_map={
                launch_station.station_id: runtime_state.t_now,
                recover_station.station_id: truck_eta,
            },
            authorized_orders=(order.order_id,),
            order_priority_band={order.order_id: coarse_plan.order_priority_band[order.order_id]},
            order_pre_score={order.order_id: coarse_plan.order_pre_score[order.order_id]},
            planner_mode_cap={order.order_id: coarse_plan.planner_mode_cap[order.order_id]},
            policy_mode_mask={order.order_id: frozenset({PolicyMode.B, PolicyMode.C})},
            route_drift_ref={
                launch_station.station_id: RouteDriftRef(
                    eta_ref=runtime_state.t_now,
                    route_index_ref=0,
                ),
                recover_station.station_id: RouteDriftRef(
                    eta_ref=truck_eta,
                    route_index_ref=1,
                )
            },
            recovery_pool={
                order.order_id: (recover_station.station_id,),
            },
            launch_candidate_stations=(launch_station.station_id,),
        )

        candidate_out = env._candidate_builder.build(
            runtime_state=runtime_state,
            coarse_plan=custom_plan,
            deciding_drone_id=drone_id,
            trigger_type="truck_station_arrival",
            trigger_station_id=launch_station.station_id,
            last_seen_plan_version=custom_plan.plan_version,
        )

        self.assertTrue(candidate_out.order_mask[0])
        self.assertEqual(candidate_out.mode_mask[0], (True, False))


if __name__ == "__main__":
    unittest.main()
