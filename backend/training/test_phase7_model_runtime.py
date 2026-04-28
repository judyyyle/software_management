#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7 model runtime tests.

运行方式：
  python -m unittest backend.training.test_phase7_model_runtime
"""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from .contracts import CriticBatch, FactorizedActionMask, ObservationBatch
from .model import SharedPPOActorCritic
from .rollout_buffer import RolloutTransition
from .train_cmrappo import (
    _build_sequence_minibatch,
    _finalize_episode_metrics,
    _materialize_recurrent_sequences,
    _TrainingConfig,
    _resolve_device,
    _set_training_seed,
    _strip_episode_details,
    _summarize_episode_records,
    _stack_lstm_states_for_update,
    _validate_meta_payload_before_write,
    torch,
)


_TORCH_AVAILABLE = torch is not None


@unittest.skipUnless(_TORCH_AVAILABLE, "缺少 torch，跳过 model runtime tests")
class TestPhase7ModelRuntime(unittest.TestCase):
    def _build_single_observation(self) -> ObservationBatch:
        return ObservationBatch(
            uav_self_token=np.asarray([0.1, 0.2, 0.3, 0.4], dtype=np.float32),
            order_tokens=np.asarray(
                [
                    [1.0, 0.1, 0.2, 0.3, 0.4],
                    [1.0, 0.5, 0.6, 0.7, 0.8],
                    [0.0, 0.0, 0.0, 0.0, 0.0],
                ],
                dtype=np.float32,
            ),
            recovery_tokens=np.asarray(
                [
                    [[1.0, 0.1, 0.2, 0.3, 0.4, 0.5], [1.0, 0.6, 0.7, 0.8, 0.9, 0.1]],
                    [[1.0, 0.2, 0.3, 0.4, 0.5, 0.6], [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
                    [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
                ],
                dtype=np.float32,
            ),
            infra_tokens=np.asarray(
                [
                    [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
                    [0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1],
                ],
                dtype=np.float32,
            ),
            history_tokens=np.asarray(
                [
                    [0.0] * 8,
                    [0.1] * 8,
                    [0.2] * 8,
                    [0.3] * 8,
                    [0.4] * 8,
                    [0.5] * 8,
                ],
                dtype=np.float32,
            ),
            history_padding_mask=np.asarray([True, False, False, False, False, False], dtype=np.bool_),
            padding_mask=np.asarray([False, False, True], dtype=np.bool_),
            recovery_padding_mask=np.asarray(
                [
                    [False, False],
                    [False, True],
                    [True, True],
                ],
                dtype=np.bool_,
            ),
        )

    def _build_single_critic_batch(self) -> CriticBatch:
        return CriticBatch(
            global_order_pool_tokens=np.asarray(
                [
                    [1.0, 0.1, 0.2, 0.3],
                    [1.0, 0.4, 0.5, 0.6],
                    [0.0, 0.0, 0.0, 0.0],
                    [0.0, 0.0, 0.0, 0.0],
                ],
                dtype=np.float32,
            ),
            global_uav_tokens=np.asarray(
                [
                    [1.0, 0.1, 0.2],
                    [1.0, 0.3, 0.4],
                    [0.0, 0.0, 0.0],
                ],
                dtype=np.float32,
            ),
            global_station_tokens=np.asarray(
                [
                    [1.0, 0.1, 0.2, 0.3, 0.4],
                    [1.0, 0.5, 0.6, 0.7, 0.8],
                ],
                dtype=np.float32,
            ),
            coarse_plan_summary_vec=np.asarray([0.1, 0.2, 0.3], dtype=np.float32),
            global_system_summary_vec=np.asarray([0.4, 0.5], dtype=np.float32),
            global_order_padding_mask=np.asarray([False, False, True, True], dtype=np.bool_),
            global_uav_padding_mask=np.asarray([False, False, True], dtype=np.bool_),
            global_station_padding_mask=np.asarray([False, False], dtype=np.bool_),
        )

    def _build_single_action_mask(self) -> FactorizedActionMask:
        return FactorizedActionMask(
            root_branch_mask=np.asarray([True, True], dtype=np.bool_),
            order_mask=np.asarray([True, True, False], dtype=np.bool_),
            mode_mask=np.asarray(
                [
                    [True, True],
                    [True, False],
                    [False, False],
                ],
                dtype=np.bool_,
            ),
            recovery_mask=np.asarray(
                [
                    [True, True],
                    [True, False],
                    [False, False],
                ],
                dtype=np.bool_,
            ),
        )

    def _build_model(self, device: torch.device) -> SharedPPOActorCritic:
        model = SharedPPOActorCritic(
            uav_feat_dim=4,
            order_feat_dim=5,
            recovery_feat_dim=6,
            infra_feat_dim=7,
            history_feat_dim=8,
            critic_order_feat_dim=4,
            critic_uav_feat_dim=3,
            critic_station_feat_dim=5,
            critic_plan_feat_dim=3,
            critic_sys_feat_dim=2,
            d_model=16,
            ff_dim=32,
            lstm_hidden=12,
            lstm_layers=1,
        )
        return model.to(device)

    def _build_training_config(self) -> _TrainingConfig:
        return _TrainingConfig(
            device="cpu",
            total_timesteps=128,
            rollout_steps=32,
            recurrent_ppo=True,
            sequence_len=2,
            burn_in_len=0,
            train_len=2,
            sequence_minibatch_size=2,
            target_minibatch_timesteps=4,
            ppo_learning_rate=3e-4,
            batch_size_alias=4,
            ppo_epochs=2,
            gamma=0.99,
            gae_lambda=0.95,
            clip_coef=0.2,
            entropy_coef=0.01,
            value_loss_coef=0.5,
            max_grad_norm=0.5,
            normalize_advantage=True,
            target_kl=0.02,
            training_seed=2026,
            log_interval_updates=1,
            save_interval_updates=1,
            eval_interval_updates=2,
            benchmark_eval_episodes=2,
            stochastic_eval_seeds=3,
            allowed_train_order_source_mode=("poisson",),
        )

    def test_forward_accepts_single_and_batched_inputs_without_extra_unsqueeze(self) -> None:
        device = torch.device("cpu")
        model = self._build_model(device)
        obs = self._build_single_observation()
        critic = self._build_single_critic_batch()
        action_mask = self._build_single_action_mask()

        single_out, _ = model.forward(
            observation_batch=obs,
            action_mask=action_mask,
            critic_batch=critic,
            lstm_state=None,
        )
        self.assertEqual(tuple(single_out.root_branch_logits.shape), (1, 2))
        self.assertEqual(tuple(single_out.order_logits.shape), (1, 3))
        self.assertEqual(tuple(single_out.mode_logits.shape), (1, 3, 2))
        self.assertEqual(tuple(single_out.recovery_logits.shape), (1, 3, 2))
        self.assertEqual(tuple(single_out.value.shape), (1,))

        batched_obs = ObservationBatch(
            uav_self_token=np.stack([obs.uav_self_token, obs.uav_self_token], axis=0),
            order_tokens=np.stack([obs.order_tokens, obs.order_tokens], axis=0),
            recovery_tokens=np.stack([obs.recovery_tokens, obs.recovery_tokens], axis=0),
            infra_tokens=np.stack([obs.infra_tokens, obs.infra_tokens], axis=0),
            history_tokens=np.stack([obs.history_tokens, obs.history_tokens], axis=0),
            history_padding_mask=np.stack([obs.history_padding_mask, obs.history_padding_mask], axis=0),
            padding_mask=np.stack([obs.padding_mask, obs.padding_mask], axis=0),
            recovery_padding_mask=np.stack(
                [obs.recovery_padding_mask, obs.recovery_padding_mask],
                axis=0,
            ),
        )
        batched_critic = CriticBatch(
            global_order_pool_tokens=np.stack(
                [critic.global_order_pool_tokens, critic.global_order_pool_tokens],
                axis=0,
            ),
            global_uav_tokens=np.stack([critic.global_uav_tokens, critic.global_uav_tokens], axis=0),
            global_station_tokens=np.stack(
                [critic.global_station_tokens, critic.global_station_tokens],
                axis=0,
            ),
            coarse_plan_summary_vec=np.stack(
                [critic.coarse_plan_summary_vec, critic.coarse_plan_summary_vec],
                axis=0,
            ),
            global_system_summary_vec=np.stack(
                [critic.global_system_summary_vec, critic.global_system_summary_vec],
                axis=0,
            ),
            global_order_padding_mask=np.stack(
                [critic.global_order_padding_mask, critic.global_order_padding_mask],
                axis=0,
            ),
            global_uav_padding_mask=np.stack(
                [critic.global_uav_padding_mask, critic.global_uav_padding_mask],
                axis=0,
            ),
            global_station_padding_mask=np.stack(
                [critic.global_station_padding_mask, critic.global_station_padding_mask],
                axis=0,
            ),
        )
        batched_action_mask = FactorizedActionMask(
            root_branch_mask=np.stack([action_mask.root_branch_mask, action_mask.root_branch_mask], axis=0),
            order_mask=np.stack([action_mask.order_mask, action_mask.order_mask], axis=0),
            mode_mask=np.stack([action_mask.mode_mask, action_mask.mode_mask], axis=0),
            recovery_mask=np.stack([action_mask.recovery_mask, action_mask.recovery_mask], axis=0),
        )

        batch_out, _ = model.forward(
            observation_batch=batched_obs,
            action_mask=batched_action_mask,
            critic_batch=batched_critic,
            lstm_state=None,
        )
        self.assertEqual(tuple(batch_out.root_branch_logits.shape), (2, 2))
        self.assertEqual(tuple(batch_out.order_logits.shape), (2, 3))
        self.assertEqual(tuple(batch_out.mode_logits.shape), (2, 3, 2))
        self.assertEqual(tuple(batch_out.recovery_logits.shape), (2, 3, 2))
        self.assertEqual(tuple(batch_out.value.shape), (2,))

    def test_forward_and_evaluate_actions_keep_outputs_on_model_device(self) -> None:
        device = _resolve_device("cpu")
        model = self._build_model(device)
        obs = self._build_single_observation()
        critic = self._build_single_critic_batch()
        action_mask = self._build_single_action_mask()

        policy_out, _ = model.forward(
            observation_batch=obs,
            action_mask=action_mask,
            critic_batch=critic,
            lstm_state=None,
        )
        self.assertEqual(policy_out.root_branch_logits.device.type, device.type)
        self.assertEqual(policy_out.value.device.type, device.type)

        action_indices = {
            "root_branch_idx": torch.as_tensor([1, 0], dtype=torch.long, device=device),
            "order_idx": torch.as_tensor([0, 0], dtype=torch.long, device=device),
            "mode_idx": torch.as_tensor([1, 0], dtype=torch.long, device=device),
            "recovery_idx": torch.as_tensor([0, 0], dtype=torch.long, device=device),
        }
        batched_obs = ObservationBatch(
            uav_self_token=np.stack([obs.uav_self_token, obs.uav_self_token], axis=0),
            order_tokens=np.stack([obs.order_tokens, obs.order_tokens], axis=0),
            recovery_tokens=np.stack([obs.recovery_tokens, obs.recovery_tokens], axis=0),
            infra_tokens=np.stack([obs.infra_tokens, obs.infra_tokens], axis=0),
            history_tokens=np.stack([obs.history_tokens, obs.history_tokens], axis=0),
            history_padding_mask=np.stack([obs.history_padding_mask, obs.history_padding_mask], axis=0),
            padding_mask=np.stack([obs.padding_mask, obs.padding_mask], axis=0),
            recovery_padding_mask=np.stack(
                [obs.recovery_padding_mask, obs.recovery_padding_mask],
                axis=0,
            ),
        )
        batched_critic = CriticBatch(
            global_order_pool_tokens=np.stack(
                [critic.global_order_pool_tokens, critic.global_order_pool_tokens],
                axis=0,
            ),
            global_uav_tokens=np.stack([critic.global_uav_tokens, critic.global_uav_tokens], axis=0),
            global_station_tokens=np.stack(
                [critic.global_station_tokens, critic.global_station_tokens],
                axis=0,
            ),
            coarse_plan_summary_vec=np.stack(
                [critic.coarse_plan_summary_vec, critic.coarse_plan_summary_vec],
                axis=0,
            ),
            global_system_summary_vec=np.stack(
                [critic.global_system_summary_vec, critic.global_system_summary_vec],
                axis=0,
            ),
            global_order_padding_mask=np.stack(
                [critic.global_order_padding_mask, critic.global_order_padding_mask],
                axis=0,
            ),
            global_uav_padding_mask=np.stack(
                [critic.global_uav_padding_mask, critic.global_uav_padding_mask],
                axis=0,
            ),
            global_station_padding_mask=np.stack(
                [critic.global_station_padding_mask, critic.global_station_padding_mask],
                axis=0,
            ),
        )
        batched_action_mask = FactorizedActionMask(
            root_branch_mask=np.stack([action_mask.root_branch_mask, action_mask.root_branch_mask], axis=0),
            order_mask=np.stack([action_mask.order_mask, action_mask.order_mask], axis=0),
            mode_mask=np.stack([action_mask.mode_mask, action_mask.mode_mask], axis=0),
            recovery_mask=np.stack([action_mask.recovery_mask, action_mask.recovery_mask], axis=0),
        )
        batch_out, _ = model.forward(
            observation_batch=batched_obs,
            action_mask=batched_action_mask,
            critic_batch=batched_critic,
            lstm_state=None,
        )
        log_prob, entropy = model.evaluate_actions(
            policy_out=batch_out,
            action_mask=batched_action_mask,
            action_indices=action_indices,
        )
        self.assertEqual(log_prob.device.type, device.type)
        self.assertEqual(entropy.device.type, device.type)

    def test_stack_lstm_states_for_update_uses_saved_transition_states(self) -> None:
        device = torch.device("cpu")
        model = self._build_model(device)
        obs = self._build_single_observation()
        critic = self._build_single_critic_batch()
        action_mask = self._build_single_action_mask()

        first_state = (
            torch.full((1, 1, 12), 1.5, dtype=torch.float32),
            torch.full((1, 1, 12), -2.0, dtype=torch.float32),
        )
        transitions = (
            RolloutTransition(
                observation_batch=obs,
                critic_batch=critic,
                action_mask=action_mask,
                action_indices=mock.Mock(),
                log_prob_old=0.0,
                value_old=0.0,
                critic_schema_hash="schema",
                reward=0.0,
                done=False,
                lstm_state_in=None,
            ),
            RolloutTransition(
                observation_batch=obs,
                critic_batch=critic,
                action_mask=action_mask,
                action_indices=mock.Mock(),
                log_prob_old=0.0,
                value_old=0.0,
                critic_schema_hash="schema",
                reward=0.0,
                done=False,
                lstm_state_in=first_state,
            ),
        )

        stacked = _stack_lstm_states_for_update(
            transitions,
            model=model,
            device=device,
        )
        self.assertIsNotNone(stacked)
        hidden, cell = stacked
        self.assertEqual(tuple(hidden.shape), (1, 2, 12))
        self.assertEqual(tuple(cell.shape), (1, 2, 12))
        self.assertTrue(torch.allclose(hidden[:, 0, :], torch.zeros((1, 12), dtype=torch.float32)))
        self.assertTrue(torch.allclose(cell[:, 0, :], torch.zeros((1, 12), dtype=torch.float32)))
        self.assertTrue(torch.allclose(hidden[:, 1, :], torch.full((1, 12), 1.5, dtype=torch.float32)))
        self.assertTrue(torch.allclose(cell[:, 1, :], torch.full((1, 12), -2.0, dtype=torch.float32)))

    def test_forward_accepts_batched_lstm_state(self) -> None:
        device = torch.device("cpu")
        model = self._build_model(device)
        obs = self._build_single_observation()
        critic = self._build_single_critic_batch()
        action_mask = self._build_single_action_mask()

        batched_obs = ObservationBatch(
            uav_self_token=np.stack([obs.uav_self_token, obs.uav_self_token], axis=0),
            order_tokens=np.stack([obs.order_tokens, obs.order_tokens], axis=0),
            recovery_tokens=np.stack([obs.recovery_tokens, obs.recovery_tokens], axis=0),
            infra_tokens=np.stack([obs.infra_tokens, obs.infra_tokens], axis=0),
            history_tokens=np.stack([obs.history_tokens, obs.history_tokens], axis=0),
            history_padding_mask=np.stack([obs.history_padding_mask, obs.history_padding_mask], axis=0),
            padding_mask=np.stack([obs.padding_mask, obs.padding_mask], axis=0),
            recovery_padding_mask=np.stack(
                [obs.recovery_padding_mask, obs.recovery_padding_mask],
                axis=0,
            ),
        )
        batched_critic = CriticBatch(
            global_order_pool_tokens=np.stack(
                [critic.global_order_pool_tokens, critic.global_order_pool_tokens],
                axis=0,
            ),
            global_uav_tokens=np.stack([critic.global_uav_tokens, critic.global_uav_tokens], axis=0),
            global_station_tokens=np.stack(
                [critic.global_station_tokens, critic.global_station_tokens],
                axis=0,
            ),
            coarse_plan_summary_vec=np.stack(
                [critic.coarse_plan_summary_vec, critic.coarse_plan_summary_vec],
                axis=0,
            ),
            global_system_summary_vec=np.stack(
                [critic.global_system_summary_vec, critic.global_system_summary_vec],
                axis=0,
            ),
            global_order_padding_mask=np.stack(
                [critic.global_order_padding_mask, critic.global_order_padding_mask],
                axis=0,
            ),
            global_uav_padding_mask=np.stack(
                [critic.global_uav_padding_mask, critic.global_uav_padding_mask],
                axis=0,
            ),
            global_station_padding_mask=np.stack(
                [critic.global_station_padding_mask, critic.global_station_padding_mask],
                axis=0,
            ),
        )
        batched_action_mask = FactorizedActionMask(
            root_branch_mask=np.stack([action_mask.root_branch_mask, action_mask.root_branch_mask], axis=0),
            order_mask=np.stack([action_mask.order_mask, action_mask.order_mask], axis=0),
            mode_mask=np.stack([action_mask.mode_mask, action_mask.mode_mask], axis=0),
            recovery_mask=np.stack([action_mask.recovery_mask, action_mask.recovery_mask], axis=0),
        )
        lstm_state = (
            torch.zeros((1, 2, 12), dtype=torch.float32, device=device),
            torch.zeros((1, 2, 12), dtype=torch.float32, device=device),
        )

        batch_out, next_state = model.forward(
            observation_batch=batched_obs,
            action_mask=batched_action_mask,
            critic_batch=batched_critic,
            lstm_state=lstm_state,
        )
        self.assertEqual(tuple(batch_out.value.shape), (2,))
        self.assertEqual(tuple(next_state[0].shape), (1, 2, 12))
        self.assertEqual(tuple(next_state[1].shape), (1, 2, 12))

    def test_forward_sequence_preserves_batch_and_time_axes(self) -> None:
        device = torch.device("cpu")
        model = self._build_model(device)
        obs = self._build_single_observation()
        critic = self._build_single_critic_batch()
        action_mask = self._build_single_action_mask()

        sequence_obs = ObservationBatch(
            uav_self_token=np.stack(
                [
                    np.stack([obs.uav_self_token, obs.uav_self_token], axis=0),
                    np.stack([obs.uav_self_token, obs.uav_self_token], axis=0),
                ],
                axis=0,
            ),
            order_tokens=np.stack(
                [
                    np.stack([obs.order_tokens, obs.order_tokens], axis=0),
                    np.stack([obs.order_tokens, obs.order_tokens], axis=0),
                ],
                axis=0,
            ),
            recovery_tokens=np.stack(
                [
                    np.stack([obs.recovery_tokens, obs.recovery_tokens], axis=0),
                    np.stack([obs.recovery_tokens, obs.recovery_tokens], axis=0),
                ],
                axis=0,
            ),
            infra_tokens=np.stack(
                [
                    np.stack([obs.infra_tokens, obs.infra_tokens], axis=0),
                    np.stack([obs.infra_tokens, obs.infra_tokens], axis=0),
                ],
                axis=0,
            ),
            history_tokens=np.stack(
                [
                    np.stack([obs.history_tokens, obs.history_tokens], axis=0),
                    np.stack([obs.history_tokens, obs.history_tokens], axis=0),
                ],
                axis=0,
            ),
            history_padding_mask=np.stack(
                [
                    np.stack([obs.history_padding_mask, obs.history_padding_mask], axis=0),
                    np.stack([obs.history_padding_mask, obs.history_padding_mask], axis=0),
                ],
                axis=0,
            ),
            padding_mask=np.stack(
                [
                    np.stack([obs.padding_mask, obs.padding_mask], axis=0),
                    np.stack([obs.padding_mask, obs.padding_mask], axis=0),
                ],
                axis=0,
            ),
            recovery_padding_mask=np.stack(
                [
                    np.stack([obs.recovery_padding_mask, obs.recovery_padding_mask], axis=0),
                    np.stack([obs.recovery_padding_mask, obs.recovery_padding_mask], axis=0),
                ],
                axis=0,
            ),
        )
        sequence_critic = CriticBatch(
            global_order_pool_tokens=np.stack(
                [
                    np.stack([critic.global_order_pool_tokens, critic.global_order_pool_tokens], axis=0),
                    np.stack([critic.global_order_pool_tokens, critic.global_order_pool_tokens], axis=0),
                ],
                axis=0,
            ),
            global_uav_tokens=np.stack(
                [
                    np.stack([critic.global_uav_tokens, critic.global_uav_tokens], axis=0),
                    np.stack([critic.global_uav_tokens, critic.global_uav_tokens], axis=0),
                ],
                axis=0,
            ),
            global_station_tokens=np.stack(
                [
                    np.stack([critic.global_station_tokens, critic.global_station_tokens], axis=0),
                    np.stack([critic.global_station_tokens, critic.global_station_tokens], axis=0),
                ],
                axis=0,
            ),
            coarse_plan_summary_vec=np.stack(
                [
                    np.stack([critic.coarse_plan_summary_vec, critic.coarse_plan_summary_vec], axis=0),
                    np.stack([critic.coarse_plan_summary_vec, critic.coarse_plan_summary_vec], axis=0),
                ],
                axis=0,
            ),
            global_system_summary_vec=np.stack(
                [
                    np.stack([critic.global_system_summary_vec, critic.global_system_summary_vec], axis=0),
                    np.stack([critic.global_system_summary_vec, critic.global_system_summary_vec], axis=0),
                ],
                axis=0,
            ),
            global_order_padding_mask=np.stack(
                [
                    np.stack([critic.global_order_padding_mask, critic.global_order_padding_mask], axis=0),
                    np.stack([critic.global_order_padding_mask, critic.global_order_padding_mask], axis=0),
                ],
                axis=0,
            ),
            global_uav_padding_mask=np.stack(
                [
                    np.stack([critic.global_uav_padding_mask, critic.global_uav_padding_mask], axis=0),
                    np.stack([critic.global_uav_padding_mask, critic.global_uav_padding_mask], axis=0),
                ],
                axis=0,
            ),
            global_station_padding_mask=np.stack(
                [
                    np.stack([critic.global_station_padding_mask, critic.global_station_padding_mask], axis=0),
                    np.stack([critic.global_station_padding_mask, critic.global_station_padding_mask], axis=0),
                ],
                axis=0,
            ),
        )
        sequence_action_mask = FactorizedActionMask(
            root_branch_mask=np.stack(
                [
                    np.stack([action_mask.root_branch_mask, action_mask.root_branch_mask], axis=0),
                    np.stack([action_mask.root_branch_mask, action_mask.root_branch_mask], axis=0),
                ],
                axis=0,
            ),
            order_mask=np.stack(
                [
                    np.stack([action_mask.order_mask, action_mask.order_mask], axis=0),
                    np.stack([action_mask.order_mask, action_mask.order_mask], axis=0),
                ],
                axis=0,
            ),
            mode_mask=np.stack(
                [
                    np.stack([action_mask.mode_mask, action_mask.mode_mask], axis=0),
                    np.stack([action_mask.mode_mask, action_mask.mode_mask], axis=0),
                ],
                axis=0,
            ),
            recovery_mask=np.stack(
                [
                    np.stack([action_mask.recovery_mask, action_mask.recovery_mask], axis=0),
                    np.stack([action_mask.recovery_mask, action_mask.recovery_mask], axis=0),
                ],
                axis=0,
            ),
        )
        policy_out, next_state = model.forward_sequence(
            observation_batch=sequence_obs,
            action_mask=sequence_action_mask,
            critic_batch=sequence_critic,
            lstm_state=None,
        )
        self.assertEqual(tuple(policy_out.root_branch_logits.shape), (2, 2, 2))
        self.assertEqual(tuple(policy_out.order_logits.shape), (2, 2, 3))
        self.assertEqual(tuple(policy_out.mode_logits.shape), (2, 2, 3, 2))
        self.assertEqual(tuple(policy_out.recovery_logits.shape), (2, 2, 3, 2))
        self.assertEqual(tuple(policy_out.value.shape), (2, 2))
        self.assertEqual(tuple(next_state[0].shape), (1, 2, 12))

    def test_materialized_sequences_group_by_actor_and_pad_tail(self) -> None:
        device = torch.device("cpu")
        model = self._build_model(device)
        obs = self._build_single_observation()
        critic = self._build_single_critic_batch()
        action_mask = self._build_single_action_mask()
        transitions = (
            RolloutTransition(
                observation_batch=obs,
                critic_batch=critic,
                action_mask=action_mask,
                action_indices=SimpleNamespace(
                    root_branch_idx=1,
                    order_idx=0,
                    mode_idx=0,
                    recovery_idx=None,
                ),
                log_prob_old=-0.1,
                value_old=0.5,
                reward=1.0,
                done=False,
                critic_schema_hash="schema",
                episode_id=0,
                actor_drone_id="uav_1",
                recurrent_segment_id=0,
                local_decision_index=0,
            ),
            RolloutTransition(
                observation_batch=obs,
                critic_batch=critic,
                action_mask=action_mask,
                action_indices=SimpleNamespace(
                    root_branch_idx=0,
                    order_idx=None,
                    mode_idx=None,
                    recovery_idx=None,
                ),
                log_prob_old=-0.2,
                value_old=0.4,
                reward=0.5,
                done=False,
                critic_schema_hash="schema",
                episode_id=0,
                actor_drone_id="uav_2",
                recurrent_segment_id=0,
                local_decision_index=0,
            ),
            RolloutTransition(
                observation_batch=obs,
                critic_batch=critic,
                action_mask=action_mask,
                action_indices=SimpleNamespace(
                    root_branch_idx=1,
                    order_idx=1,
                    mode_idx=1,
                    recovery_idx=0,
                ),
                log_prob_old=-0.3,
                value_old=0.3,
                reward=0.2,
                done=True,
                critic_schema_hash="schema",
                episode_id=0,
                actor_drone_id="uav_1",
                recurrent_segment_id=0,
                local_decision_index=1,
            ),
        )
        batch_view = SimpleNamespace(
            transitions=transitions,
            advantages=np.asarray([1.0, 2.0, 3.0], dtype=np.float32),
            returns=np.asarray([1.5, 2.5, 3.5], dtype=np.float32),
            rewards=np.asarray([1.0, 0.5, 0.2], dtype=np.float32),
        )

        sequences = _materialize_recurrent_sequences(
            batch_view=batch_view,
            sequence_len=2,
        )
        self.assertEqual(len(sequences), 2)
        self.assertEqual(sequences[0].sequence_id, (0, "uav_1", 0, 0))
        self.assertEqual(len(sequences[0].transitions), 2)
        self.assertEqual(sequences[1].sequence_id, (0, "uav_2", 0, 0))
        self.assertEqual(len(sequences[1].transitions), 1)

        minibatch = _build_sequence_minibatch(
            sequences=sequences,
            train_cfg=self._build_training_config(),
            model=model,
            device=device,
        )
        self.assertEqual(tuple(minibatch.valid_timestep_mask.shape), (2, 2))
        self.assertTrue(bool(minibatch.valid_timestep_mask[0, 0].item()))
        self.assertTrue(bool(minibatch.valid_timestep_mask[0, 1].item()))
        self.assertTrue(bool(minibatch.valid_timestep_mask[1, 0].item()))
        self.assertFalse(bool(minibatch.valid_timestep_mask[1, 1].item()))
        self.assertEqual(minibatch.padded_timesteps, 1)
        self.assertEqual(int(minibatch.action_indices["root_branch_idx"][1, 1].item()), 0)

    def test_resolve_device_auto_prefers_mps_after_cuda(self) -> None:
        if not hasattr(torch.backends, "mps"):
            self.skipTest("当前 torch 构建不包含 MPS backend")
        with mock.patch("backend.training.train_cmrappo.torch.cuda.is_available", return_value=False):
            with mock.patch("backend.training.train_cmrappo.torch.backends.mps.is_available", return_value=True):
                device = _resolve_device("auto")
        self.assertEqual(device.type, "mps")

    def test_set_training_seed_replays_python_numpy_and_torch_streams(self) -> None:
        _set_training_seed(20260427)
        python_seq_1 = [random.random() for _ in range(3)]
        numpy_seq_1 = np.random.rand(3)
        torch_seq_1 = torch.rand(3)

        _set_training_seed(20260427)
        python_seq_2 = [random.random() for _ in range(3)]
        numpy_seq_2 = np.random.rand(3)
        torch_seq_2 = torch.rand(3)

        self.assertEqual(python_seq_1, python_seq_2)
        self.assertTrue(np.allclose(numpy_seq_1, numpy_seq_2))
        self.assertTrue(torch.allclose(torch_seq_1, torch_seq_2))

    def test_finalize_episode_metrics_merges_runtime_snapshot(self) -> None:
        payload = _finalize_episode_metrics(
            accumulator=SimpleNamespace(
                phase="train",
                episode_id=7,
                order_source_mode="poisson",
                order_source_seed=20260424,
                total_reward=12.5,
                decision_count=9,
                global_step_start=128,
                update_start=3,
            ),
            episode_snapshot={
                "episode_end_t_sec": 3600.0,
                "delivery_count": 4,
                "fallback_count": 1,
                "hard_failure_count": 0,
                "reservation_timeout_count": 2,
            },
            global_step_end=137,
            update_idx_end=4,
        )
        self.assertEqual(payload["phase"], "train")
        self.assertEqual(payload["episode_id"], 7)
        self.assertEqual(payload["order_source_mode"], "poisson")
        self.assertEqual(payload["episode_length_decisions"], 9)
        self.assertEqual(payload["delivery_count"], 4)
        self.assertEqual(payload["global_step_end"], 137)
        self.assertEqual(payload["update_end"], 4)

    def test_summarize_episode_records_aggregates_counts_and_ratios(self) -> None:
        report = _summarize_episode_records(
            split="benchmark",
            order_source_mode="benchmark",
            episodes=[
                {
                    "total_reward": 10.0,
                    "episode_length_decisions": 5,
                    "episode_end_t_sec": 120.0,
                    "delivery_count": 2,
                    "on_time_rate": 1.0,
                    "timeout_order_count": 0,
                    "fallback_count": 1,
                    "hard_failure_count": 0,
                    "reservation_timeout_count": 0,
                    "hard_overdue_count": 0,
                    "t_wait_sec": 3.0,
                    "t_idle_sec": 1.0,
                    "t_queue_sec": 2.0,
                    "t_fallback_sec": 4.0,
                    "t_overdue_sec": 0.0,
                    "t_reservation_timeout_cost_sec": 0.0,
                    "dispatch_mode_b_count": 3,
                    "dispatch_mode_c_count": 1,
                },
                {
                    "total_reward": 14.0,
                    "episode_length_decisions": 7,
                    "episode_end_t_sec": 150.0,
                    "delivery_count": 3,
                    "on_time_rate": 0.5,
                    "timeout_order_count": 1,
                    "fallback_count": 2,
                    "hard_failure_count": 1,
                    "reservation_timeout_count": 1,
                    "hard_overdue_count": 1,
                    "t_wait_sec": 5.0,
                    "t_idle_sec": 2.0,
                    "t_queue_sec": 1.0,
                    "t_fallback_sec": 6.0,
                    "t_overdue_sec": 7.0,
                    "t_reservation_timeout_cost_sec": 8.0,
                    "dispatch_mode_b_count": 1,
                    "dispatch_mode_c_count": 3,
                },
            ],
            update_idx=5,
            global_step=1024,
            generated_at="2026-04-27T10:20:30+00:00",
        )
        self.assertEqual(report["episode_count"], 2)
        self.assertEqual(report["sum_delivery_count"], 5)
        self.assertEqual(report["sum_fallback_count"], 3)
        self.assertEqual(report["sum_hard_failure_count"], 1)
        self.assertAlmostEqual(report["mean_total_reward"], 12.0)
        self.assertAlmostEqual(report["mode_b_dispatch_ratio"], 0.5)
        self.assertAlmostEqual(report["mode_c_dispatch_ratio"], 0.5)
        self.assertEqual(len(report["episodes"]), 2)

    def test_strip_episode_details_removes_verbose_episode_list(self) -> None:
        compact = _strip_episode_details(
            {
                "split": "benchmark",
                "mean_total_reward": 1.0,
                "episodes": [{"episode_id": 0}],
            }
        )
        self.assertEqual(compact["split"], "benchmark")
        self.assertEqual(compact["mean_total_reward"], 1.0)
        self.assertNotIn("episodes", compact)

    def test_validate_meta_payload_before_write_accepts_consistent_runtime_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            policy_path = tmp_dir / "policy.pt"
            policy_path.write_bytes(b"policy")
            config_snapshot_path = tmp_dir / "training_config_snapshot.yaml"
            config_snapshot_path.write_text("training: {}\n", encoding="utf-8")
            metrics_path = tmp_dir / "train_metrics.jsonl"
            metrics_path.write_text("{\"update\": 1}\n", encoding="utf-8")

            scene_ctx = SimpleNamespace(
                scene_id="scene_a",
                scene_bundle_dir="/tmp/scene_bundle",
            )
            benchmark = SimpleNamespace(
                orders_json="backend/data/orders.json",
                orders_json_sha256="abc123",
                static_order_count=10,
                dynamic_order_count=5,
                benchmark_use_dynamic_orders=True,
            )
            training_input_meta = SimpleNamespace(
                poisson_arrival_rate=2.5,
                poisson_weight_max_kg=10.0,
                order_window_min_min=20,
                order_window_max_min=60,
            )
            order_source = SimpleNamespace(
                mode=SimpleNamespace(value="poisson"),
                seed=314159,
                benchmark=benchmark,
                training_input_meta=training_input_meta,
            )
            critic_schema = SimpleNamespace(schema_hash="schema_hash_v1")
            train_cfg = self._build_training_config()
            meta_payload = {
                "model_version": "cmrappo_test",
                "trained_at": "2026-04-27T10:20:30+00:00",
                "scene_id": "scene_a",
                "scene_bundle_dir": "/tmp/scene_bundle",
                "training_input": {
                    "order_source_mode": "poisson",
                    "benchmark": {
                        "orders_json": "backend/data/orders.json",
                        "orders_json_sha256": "abc123",
                        "static_order_count": 10,
                        "dynamic_order_count": 5,
                        "benchmark_use_dynamic_orders": True,
                    },
                    "poisson_arrival_rate": 2.5,
                    "poisson_weight_max_kg": 10.0,
                    "order_window_min_min": 20,
                    "order_window_max_min": 60,
                    "poisson_seed": 314159,
                    "training_seed": 2026,
                    "total_timesteps": 128,
                },
                "policy": {"encoder_type": "mlp"},
                "action_space": {"type": "factorized"},
                "candidate": {"max_candidate_orders": 32},
                "planner": {"coarse_replan_interval_sec": 60.0},
                "reward": {"lambda_wait": 1.0},
                "critic_schema": {
                    "name": "critic_tensor_v1",
                    "schema_version": "v1",
                    "schema_hash": "schema_hash_v1",
                },
                "shared_runtime_params_snapshot": {
                    "source_config": "backend/config/drone_params.yaml",
                    "light_drone": {"k1": 1.0},
                    "heavy_drone": {"k1": 2.0},
                    "solver_energy": {"lambda_time": 1.0},
                },
                "env_semantic_contract": {"fifo_queue_enabled": True},
                "online_lock_params": {"locked_fields": [], "tunable_fields": []},
            }

            _validate_meta_payload_before_write(
                meta_payload=meta_payload,
                scene_ctx=scene_ctx,
                order_source=order_source,
                critic_schema=critic_schema,
                train_cfg=train_cfg,
                model_version="cmrappo_test",
                global_step=128,
                policy_path=policy_path,
                config_snapshot_path=config_snapshot_path,
                metrics_path=metrics_path,
            )

    def test_validate_meta_payload_before_write_rejects_incomplete_training(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            policy_path = tmp_dir / "policy.pt"
            policy_path.write_bytes(b"policy")
            config_snapshot_path = tmp_dir / "training_config_snapshot.yaml"
            config_snapshot_path.write_text("training: {}\n", encoding="utf-8")
            metrics_path = tmp_dir / "train_metrics.jsonl"
            metrics_path.write_text("{\"update\": 1}\n", encoding="utf-8")

            scene_ctx = SimpleNamespace(scene_id="scene_a", scene_bundle_dir="/tmp/scene_bundle")
            order_source = SimpleNamespace(
                mode=SimpleNamespace(value="poisson"),
                seed=1,
                benchmark=SimpleNamespace(
                    orders_json="o",
                    orders_json_sha256="h",
                    static_order_count=1,
                    dynamic_order_count=1,
                    benchmark_use_dynamic_orders=True,
                ),
                training_input_meta=SimpleNamespace(
                    poisson_arrival_rate=1.0,
                    poisson_weight_max_kg=10.0,
                    order_window_min_min=20,
                    order_window_max_min=60,
                ),
            )
            critic_schema = SimpleNamespace(schema_hash="schema_hash_v1")
            train_cfg = self._build_training_config()
            meta_payload = {
                "model_version": "cmrappo_test",
                "trained_at": "2026-04-27T10:20:30+00:00",
                "scene_id": "scene_a",
                "scene_bundle_dir": "/tmp/scene_bundle",
                "training_input": {
                    "order_source_mode": "poisson",
                    "benchmark": {
                        "orders_json": "o",
                        "orders_json_sha256": "h",
                        "static_order_count": 1,
                        "dynamic_order_count": 1,
                        "benchmark_use_dynamic_orders": True,
                    },
                    "poisson_arrival_rate": 1.0,
                    "poisson_weight_max_kg": 10.0,
                    "order_window_min_min": 20,
                    "order_window_max_min": 60,
                    "poisson_seed": 1,
                    "training_seed": 2026,
                    "total_timesteps": 128,
                },
                "policy": {"encoder_type": "mlp"},
                "action_space": {"type": "factorized"},
                "candidate": {"max_candidate_orders": 32},
                "planner": {"coarse_replan_interval_sec": 60.0},
                "reward": {"lambda_wait": 1.0},
                "critic_schema": {
                    "name": "critic_tensor_v1",
                    "schema_version": "v1",
                    "schema_hash": "schema_hash_v1",
                },
                "shared_runtime_params_snapshot": {
                    "source_config": "backend/config/drone_params.yaml",
                    "light_drone": {"k1": 1.0},
                    "heavy_drone": {"k1": 2.0},
                    "solver_energy": {"lambda_time": 1.0},
                },
                "env_semantic_contract": {"fifo_queue_enabled": True},
                "online_lock_params": {"locked_fields": [], "tunable_fields": []},
            }

            with self.assertRaises(RuntimeError):
                _validate_meta_payload_before_write(
                    meta_payload=meta_payload,
                    scene_ctx=scene_ctx,
                    order_source=order_source,
                    critic_schema=critic_schema,
                    train_cfg=train_cfg,
                    model_version="cmrappo_test",
                    global_step=127,
                    policy_path=policy_path,
                    config_snapshot_path=config_snapshot_path,
                    metrics_path=metrics_path,
                )


if __name__ == "__main__":
    unittest.main()
