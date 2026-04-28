#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7 snapshot / tensorizer / critic buffer tests.

运行方式：
  python -m unittest backend.training.test_phase7_snapshot_and_tensorizers
"""

from __future__ import annotations

import unittest

import numpy as np

from .critic_batch_builder import CriticBatchBuilder
from .contracts import ResolvedActionIndices
from .env_adapter import TrainingEnvAdapter
from .observation_tensorizer import ObservationTensorizer
from .rollout_buffer import RolloutBuffer, compute_gae


class TestPhase7SnapshotAndTensorizers(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TrainingEnvAdapter()
        self.reset_result = self.env.reset()
        self.assertIsNotNone(self.reset_result.decision_context)
        self.decision = self.reset_result.decision_context
        self.tensorizer = ObservationTensorizer()
        self.critic_builder = CriticBatchBuilder()

    def test_decision_context_exposes_phase7_pre_action_snapshot(self) -> None:
        planner_snapshot = self.decision.planner_snapshot
        execution_snapshot = self.decision.execution_snapshot

        self.assertEqual(self.decision.t_decision, self.decision.runtime_state.t_now)
        self.assertIn(self.decision.deciding_drone_id, execution_snapshot.uav_eta_to_available)
        self.assertIn(self.decision.deciding_drone_id, execution_snapshot.uav_dispatch_mode)
        self.assertGreaterEqual(planner_snapshot.backlog_new_orders, 0)
        self.assertGreaterEqual(planner_snapshot.fallback_count_in_window, 0)
        self.assertGreaterEqual(planner_snapshot.hard_failure_count_in_window, 0)
        self.assertGreaterEqual(planner_snapshot.total_backbone_count, 0)
        self.assertIsInstance(planner_snapshot.active_launch_stations, tuple)

    def test_observation_tensorizer_builds_fixed_shape_batches(self) -> None:
        candidate_out = self.env.build_candidate_output(
            self.decision,
            last_seen_plan_version=self.decision.coarse_plan.plan_version,
        )
        batch = self.tensorizer.build(
            decision_context=self.decision,
            candidate_out=candidate_out,
            transition_history=(),
        )
        action_mask = self.tensorizer.build_action_mask(candidate_out)

        self.assertEqual(batch.order_tokens.shape[0], len(candidate_out.order_mask))
        self.assertEqual(batch.recovery_tokens.shape[:2], batch.recovery_padding_mask.shape)
        self.assertEqual(batch.history_tokens.shape[0], self.tensorizer.history_length)
        self.assertEqual(batch.history_padding_mask.dtype, np.bool_)
        self.assertEqual(action_mask.root_branch_mask.shape, (2,))
        self.assertEqual(action_mask.mode_mask.shape[0], len(candidate_out.mode_mask))

    def test_critic_batch_builder_respects_schema_capacity(self) -> None:
        schema = self.critic_builder.default_schema_meta
        critic_batch = self.critic_builder.build(
            decision_context=self.decision,
            critic_tensor_schema_meta=schema,
        )

        self.assertEqual(
            critic_batch.global_order_pool_tokens.shape,
            (schema.max_global_orders, len(schema.order_token_fields)),
        )
        self.assertEqual(
            critic_batch.global_uav_tokens.shape,
            (schema.max_global_uavs, len(schema.uav_token_fields)),
        )
        self.assertEqual(
            critic_batch.global_station_tokens.shape,
            (schema.max_global_stations, len(schema.station_token_fields)),
        )
        self.assertEqual(
            critic_batch.coarse_plan_summary_vec.shape,
            (len(schema.coarse_plan_summary_fields),),
        )
        self.assertEqual(
            critic_batch.global_system_summary_vec.shape,
            (len(schema.global_system_summary_fields),),
        )
        self.assertTrue(schema.schema_hash)

    def test_rollout_buffer_two_phase_finalize_and_gae(self) -> None:
        candidate_out = self.env.build_candidate_output(
            self.decision,
            last_seen_plan_version=self.decision.coarse_plan.plan_version,
        )
        observation_batch = self.tensorizer.build(
            decision_context=self.decision,
            candidate_out=candidate_out,
            transition_history=(),
        )
        action_mask = self.tensorizer.build_action_mask(candidate_out)
        critic_batch = self.critic_builder.build(
            decision_context=self.decision,
            critic_tensor_schema_meta=self.critic_builder.default_schema_meta,
        )

        buffer = RolloutBuffer(capacity=2)
        slot = buffer.begin_transition(
            observation_batch=observation_batch,
            critic_batch=critic_batch,
            action_mask=action_mask,
            action_indices=ResolvedActionIndices(root_branch_idx=0),
            log_prob_old=-0.1,
            value_old=1.0,
            critic_schema_hash=self.critic_builder.default_schema_meta.schema_hash,
        )
        buffer.finalize_transition(slot, reward=2.0, done=False)

        self.assertEqual(buffer.size, 1)
        view = buffer.build_batch_view(last_value=0.5, gamma=0.99, gae_lambda=0.95)
        self.assertEqual(view.rewards.shape, (1,))
        self.assertEqual(view.advantages.shape, (1,))
        self.assertEqual(view.returns.shape, (1,))

        advantages, returns = compute_gae(
            rewards=np.asarray([2.0], dtype=np.float32),
            dones=np.asarray([False], dtype=np.bool_),
            values=np.asarray([1.0], dtype=np.float32),
            last_value=0.5,
            gamma=0.99,
            gae_lambda=0.95,
        )
        self.assertAlmostEqual(float(view.advantages[0]), float(advantages[0]), places=6)
        self.assertAlmostEqual(float(view.returns[0]), float(returns[0]), places=6)


if __name__ == "__main__":
    unittest.main()
