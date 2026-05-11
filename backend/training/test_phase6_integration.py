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

from .contracts import PlannerTriggerContext, PolicyMode, RouteDriftRef
from .env_adapter import (
    BackboneVisit,
    PlannedStop,
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

    def test_planner_bridge_recovery_pool_scans_future_backbone_then_selects_top_k(self) -> None:
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

        self.assertEqual(len(recovery_nodes), env._cfg.max_candidate_recovery_per_order)
        self.assertIn(
            preferred_station_id,
            recovery_nodes,
            "PlannerBridge 应与 env 内联 coarse plan 一致，允许后续优质节点进入 recovery_pool",
        )
        self.assertNotEqual(
            recovery_nodes,
            tuple(station_ids[: env._cfg.max_candidate_recovery_per_order]),
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
            recovery_idx=0,
        )
        self.assertEqual(resolved_wait, WAIT_ACTION)
        self.assertEqual(resolved_mode_c.order_id, order.order_id)
        self.assertEqual(resolved_mode_c.mode, PolicyMode.C)
        self.assertEqual(resolved_mode_c.recover_node_id, station_safe.station_id)

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
                station_1.station_id: runtime_state.t_now + 1800.0,
                station_2.station_id: runtime_state.t_now + 2400.0,
            },
            route_drift_ref={
                station_1.station_id: RouteDriftRef(
                    eta_ref=runtime_state.t_now + 1800.0,
                    route_index_ref=0,
                ),
                station_2.station_id: RouteDriftRef(
                    eta_ref=runtime_state.t_now + 2400.0,
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
        self.assertFalse(any(candidate_out.recovery_mask[0]))
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
                recovery_idx=0,
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
        self.assertFalse(any(candidate_out.recovery_mask[0]))


if __name__ == "__main__":
    unittest.main()
