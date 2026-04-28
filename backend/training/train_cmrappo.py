#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — Phase 7 PPO 训练主循环。

当前实现目标：
  - 复用 Phase 2~6 已冻结的 `DecisionContext -> CandidateOutput -> EnvAction` 链路；
  - 在训练前强制校验 `order_source_mode == poisson`；
  - 使用 `ObservationTensorizer` / `CriticBatchBuilder` / `RolloutBuffer`
    固化 Phase 7 的 pre-action snapshot 训练数据流；
  - 训练产物写出 `policy.pt` / `meta.json` / `train_metrics.jsonl` / 配置快照。
"""

from __future__ import annotations

import json
import random
import shutil
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from config.loader import load_drone_params

from .contracts import (
    ActionSpaceMeta,
    CandidateMeta,
    CriticTensorSchemaMeta,
    DroneRuntimeParamsSnapshot,
    EnvSemanticContractMeta,
    MetaJson,
    OnlineLockParams,
    PlannerMeta,
    PolicyMeta,
    ResolvedActionIndices,
    RewardMeta,
    SharedRuntimeParamsSnapshot,
    SolverEnergyRuntimeSnapshot,
    TrainingRunMeta,
    build_meta_json_dict,
)
from .critic_batch_builder import CriticBatchBuilder
from .model import SharedPPOActorCritic
from .observation_tensorizer import ObservationTensorizer
from .order_source_adapter import OrderSourceMode, build_order_source, ensure_mode_allowed
from .rollout_buffer import RolloutBuffer, RolloutTransition
from .scene_loader import DEFAULT_CONFIG_PATH, load_default_scene
from .env_adapter import TrainingEnvAdapter


try:  # pragma: no cover
    import torch
except ImportError:  # pragma: no cover
    torch = None


@dataclass(frozen=True)
class _TrainingConfig:
    device: str
    total_timesteps: int
    rollout_steps: int
    recurrent_ppo: bool
    sequence_len: int
    burn_in_len: int
    train_len: int
    sequence_minibatch_size: int
    target_minibatch_timesteps: int
    ppo_learning_rate: float
    batch_size_alias: int | None
    ppo_epochs: int
    gamma: float
    gae_lambda: float
    clip_coef: float
    vf_clip_coef: float
    entropy_coef: float
    value_loss_coef: float
    max_grad_norm: float
    normalize_advantage: bool
    target_kl: float
    training_seed: int
    log_interval_updates: int
    save_interval_updates: int
    eval_interval_updates: int
    benchmark_eval_episodes: int
    stochastic_eval_seeds: int
    allowed_train_order_source_mode: tuple[str, ...]


@dataclass(frozen=True)
class _PolicyConfig:
    d_model: int
    ff_dim: int
    lstm_hidden: int
    lstm_layers: int
    hist_len: int


@dataclass(frozen=True)
class _EvalConfig:
    benchmark_seed: int
    stochastic_seed_base: int
    stochastic_arrival_rates: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class _MaterializedSequence:
    sequence_id: tuple[int, str, int, int]
    episode_id: int
    actor_drone_id: str
    recurrent_segment_id: int
    local_chunk_idx: int
    transitions: tuple[RolloutTransition, ...]
    advantages: np.ndarray
    returns: np.ndarray
    valid_timestep_mask: np.ndarray


@dataclass(frozen=True)
class _SequenceMiniBatch:
    observation_batch: Any
    critic_batch: Any
    action_mask: Any
    action_indices: dict[str, Any]
    old_log_probs: Any
    old_values: Any
    returns: Any
    advantages: Any
    valid_timestep_mask: Any
    lstm_state_in: Any | None
    sequence_count: int
    padded_timesteps: int


@dataclass
class _EpisodeAccumulator:
    phase: str
    episode_id: int
    order_source_mode: str
    order_source_seed: int
    global_step_start: int | None = None
    global_step_end: int | None = None
    update_start: int | None = None
    update_end: int | None = None
    decision_count: int = 0
    total_reward: float = 0.0


def train_cmrappo(
    *,
    output_dir: str | Path,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    model_version: str | None = None,
) -> dict[str, Any]:
    _require_torch()
    scene_ctx = load_default_scene(config_path=config_path)
    train_cfg = _load_training_config(Path(config_path))
    policy_cfg = _load_policy_config(Path(config_path))
    eval_cfg = _load_eval_config(Path(config_path))
    _set_training_seed(train_cfg.training_seed)
    order_source = build_order_source(
        scene_ctx,
        mode=OrderSourceMode.POISSON,
        config_path=config_path,
    )
    ensure_mode_allowed(
        order_source.mode,
        train_cfg.allowed_train_order_source_mode,
        "train_cmrappo",
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(config_path), out_dir / "training_config_snapshot.yaml")

    env = TrainingEnvAdapter(
        scene_ctx=scene_ctx,
        order_source=order_source,
        config_path=config_path,
    )
    tensorizer = ObservationTensorizer(scene_ctx=scene_ctx, config_path=config_path)
    critic_builder = CriticBatchBuilder(scene_ctx=scene_ctx, config_path=config_path)
    critic_schema = critic_builder.default_schema_meta

    initial_result = env.reset()
    if initial_result.decision_context is None:
        raise RuntimeError("reset 后没有可用的 decision_context，无法启动训练")

    last_seen_plan_version_by_drone: dict[str, int] = {}
    history_buffer: deque[Any] = deque(maxlen=policy_cfg.hist_len)
    lstm_state_by_drone: dict[str, Any] = {}
    recurrent_segment_id_by_drone: dict[str, int] = {}
    local_decision_cursor: dict[tuple[int, str, int], int] = {}
    episode_id = 0
    bootstrap_candidate = _build_candidate_output(
        env=env,
        decision_context=initial_result.decision_context,
        last_seen_plan_version_by_drone=last_seen_plan_version_by_drone,
    )
    bootstrap_observation = tensorizer.build(
        decision_context=initial_result.decision_context,
        candidate_out=bootstrap_candidate,
        transition_history=tuple(history_buffer),
    )
    bootstrap_action_mask = tensorizer.build_action_mask(bootstrap_candidate)
    bootstrap_critic = critic_builder.build(
        decision_context=initial_result.decision_context,
        critic_tensor_schema_meta=critic_schema,
    )
    model = SharedPPOActorCritic(
        uav_feat_dim=int(np.asarray(bootstrap_observation.uav_self_token).shape[-1]),
        order_feat_dim=int(np.asarray(bootstrap_observation.order_tokens).shape[-1]),
        recovery_feat_dim=int(np.asarray(bootstrap_observation.recovery_tokens).shape[-1]),
        infra_feat_dim=int(np.asarray(bootstrap_observation.infra_tokens).shape[-1]),
        history_feat_dim=int(np.asarray(bootstrap_observation.history_tokens).shape[-1]),
        critic_order_feat_dim=int(np.asarray(bootstrap_critic.global_order_pool_tokens).shape[-1]),
        critic_uav_feat_dim=int(np.asarray(bootstrap_critic.global_uav_tokens).shape[-1]),
        critic_station_feat_dim=int(np.asarray(bootstrap_critic.global_station_tokens).shape[-1]),
        critic_plan_feat_dim=int(np.asarray(bootstrap_critic.coarse_plan_summary_vec).shape[-1]),
        critic_sys_feat_dim=int(np.asarray(bootstrap_critic.global_system_summary_vec).shape[-1]),
        d_model=policy_cfg.d_model,
        ff_dim=policy_cfg.ff_dim,
        lstm_hidden=policy_cfg.lstm_hidden,
        lstm_layers=policy_cfg.lstm_layers,
    )
    device = _resolve_device(train_cfg.device)
    model.to(device)
    _emit_runtime_device_debug(
        requested_device=train_cfg.device,
        resolved_device=device,
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=train_cfg.ppo_learning_rate)

    metrics_path = out_dir / "train_metrics.jsonl"
    episode_metrics_path = out_dir / "episode_metrics.jsonl"
    eval_metrics_path = out_dir / "eval_metrics.jsonl"
    benchmark_report_path = out_dir / "benchmark_report.json"
    stochastic_report_path = out_dir / "stochastic_report.json"
    policy_path = out_dir / "policy.pt"
    meta_path = out_dir / "meta.json"
    config_snapshot_path = out_dir / "training_config_snapshot.yaml"
    model_version = model_version or datetime.now(timezone.utc).strftime("cmrappo_%Y%m%dT%H%M%SZ")

    global_step = 0
    update_idx = 0
    current_result = initial_result
    episode_acc = _EpisodeAccumulator(
        phase="train",
        episode_id=episode_id,
        order_source_mode=str(order_source.mode.value),
        order_source_seed=int(order_source.seed),
        global_step_start=0,
        update_start=0,
    )
    while global_step < train_cfg.total_timesteps:
        rollout = RolloutBuffer(capacity=train_cfg.rollout_steps)
        while rollout.size < rollout.capacity and global_step < train_cfg.total_timesteps:
            if current_result.done:
                current_result = env.reset()
                episode_id += 1
                episode_acc = _EpisodeAccumulator(
                    phase="train",
                    episode_id=episode_id,
                    order_source_mode=str(order_source.mode.value),
                    order_source_seed=int(order_source.seed),
                    global_step_start=global_step,
                    update_start=update_idx,
                )
                history_buffer.clear()
                lstm_state_by_drone.clear()
                recurrent_segment_id_by_drone.clear()
                local_decision_cursor.clear()
            decision_context = current_result.decision_context
            if decision_context is None:
                current_result = env.reset()
                episode_id += 1
                episode_acc = _EpisodeAccumulator(
                    phase="train",
                    episode_id=episode_id,
                    order_source_mode=str(order_source.mode.value),
                    order_source_seed=int(order_source.seed),
                    global_step_start=global_step,
                    update_start=update_idx,
                )
                history_buffer.clear()
                lstm_state_by_drone.clear()
                recurrent_segment_id_by_drone.clear()
                local_decision_cursor.clear()
                continue

            drone_id = str(decision_context.deciding_drone_id)
            lstm_state_in = lstm_state_by_drone.get(drone_id)
            recurrent_segment_id = recurrent_segment_id_by_drone.get(drone_id, 0)
            decision_key = (episode_id, drone_id, recurrent_segment_id)
            local_decision_index = local_decision_cursor.get(decision_key, 0)
            candidate_out = _build_candidate_output(
                env=env,
                decision_context=decision_context,
                last_seen_plan_version_by_drone=last_seen_plan_version_by_drone,
            )
            observation_batch = tensorizer.build(
                decision_context=decision_context,
                candidate_out=candidate_out,
                transition_history=tuple(history_buffer),
            )
            action_mask = tensorizer.build_action_mask(candidate_out)
            critic_batch = critic_builder.build(
                decision_context=decision_context,
                critic_tensor_schema_meta=critic_schema,
            )
            with torch.no_grad():
                policy_out, next_lstm_state = model.forward(
                    observation_batch=observation_batch,
                    action_mask=action_mask,
                    critic_batch=critic_batch,
                    lstm_state=lstm_state_in,
                )
                sampled_action, log_prob = model.sample_action(
                    policy_out=policy_out,
                    action_mask=action_mask,
                    deterministic=False,
                )
            action_indices = ResolvedActionIndices(**sampled_action)
            env_action = candidate_out.resolved_action_lookup.resolve(
                root_branch_idx=action_indices.root_branch_idx,
                order_idx=action_indices.order_idx,
                mode_idx=action_indices.mode_idx,
                recovery_idx=action_indices.recovery_idx,
            )
            slot = rollout.begin_transition(
                observation_batch=observation_batch,
                critic_batch=critic_batch,
                action_mask=action_mask,
                action_indices=action_indices,
                log_prob_old=float(log_prob.detach().cpu().item()),
                value_old=float(policy_out.value.detach().cpu().item()),
                critic_schema_hash=critic_schema.schema_hash,
                lstm_state_in=_detach_lstm_state(lstm_state=lstm_state_in),
                episode_id=episode_id,
                actor_drone_id=drone_id,
                recurrent_segment_id=recurrent_segment_id,
                local_decision_index=local_decision_index,
                decision_context_debug_snapshot=decision_context,
            )
            step_result = env.step(env_action)
            rollout.finalize_transition(
                slot,
                reward=float(step_result.reward),
                done=bool(step_result.done),
                lstm_state_out=_detach_lstm_state(lstm_state=next_lstm_state),
            )
            history_buffer.append(
                tensorizer.build_transition_summary(
                    decision_context=decision_context,
                    candidate_out=candidate_out,
                    action_indices=action_indices,
                    step_result=step_result,
                )
            )
            last_seen_plan_version_by_drone[decision_context.deciding_drone_id] = (
                decision_context.coarse_plan.plan_version
            )
            local_decision_cursor[decision_key] = local_decision_index + 1
            lstm_state_by_drone[drone_id] = _detach_lstm_state(lstm_state=next_lstm_state)
            _reset_recurrent_state_for_failed_drones(
                runtime_state=step_result.runtime_state,
                lstm_state_by_drone=lstm_state_by_drone,
                recurrent_segment_id_by_drone=recurrent_segment_id_by_drone,
            )
            current_result = step_result
            global_step += 1
            _record_episode_step(episode_acc, reward=float(step_result.reward))
            if step_result.done:
                _append_metrics(
                    episode_metrics_path,
                    _finalize_episode_metrics(
                        accumulator=episode_acc,
                        episode_snapshot=env.build_episode_metrics_snapshot(),
                        global_step_end=global_step,
                        update_idx_end=update_idx,
                    ),
                )

        last_value = 0.0
        if not current_result.done and current_result.decision_context is not None:
            next_drone_id = str(current_result.decision_context.deciding_drone_id)
            next_candidate = _build_candidate_output(
                env=env,
                decision_context=current_result.decision_context,
                last_seen_plan_version_by_drone=last_seen_plan_version_by_drone,
            )
            next_observation = tensorizer.build(
                decision_context=current_result.decision_context,
                candidate_out=next_candidate,
                transition_history=tuple(history_buffer),
            )
            next_action_mask = tensorizer.build_action_mask(next_candidate)
            next_critic_batch = critic_builder.build(
                decision_context=current_result.decision_context,
                critic_tensor_schema_meta=critic_schema,
            )
            with torch.no_grad():
                next_policy_out, _ = model.forward(
                    observation_batch=next_observation,
                    action_mask=next_action_mask,
                    critic_batch=next_critic_batch,
                    lstm_state=lstm_state_by_drone.get(next_drone_id),
                )
            last_value = float(next_policy_out.value.detach().cpu().item())

        batch_view = rollout.build_batch_view(
            last_value=last_value,
            gamma=train_cfg.gamma,
            gae_lambda=train_cfg.gae_lambda,
        )
        ppo_stats = _ppo_update(
            model=model,
            optimizer=optimizer,
            batch_view=batch_view,
            train_cfg=train_cfg,
            device=device,
        )
        update_idx += 1
        _append_metrics(
            metrics_path,
            {
                "update": update_idx,
                "global_step": global_step,
                "rollout_size": rollout.size,
                **ppo_stats,
            },
        )
        if (
            train_cfg.eval_interval_updates > 0
            and (
                update_idx % train_cfg.eval_interval_updates == 0
                or global_step >= train_cfg.total_timesteps
            )
        ):
            eval_report = _run_periodic_evaluation(
                scene_ctx=scene_ctx,
                config_path=Path(config_path),
                model=model,
                tensorizer=tensorizer,
                critic_builder=critic_builder,
                critic_schema=critic_schema,
                policy_cfg=policy_cfg,
                eval_cfg=eval_cfg,
                train_cfg=train_cfg,
                device=device,
                update_idx=update_idx,
                global_step=global_step,
            )
            _append_metrics(
                eval_metrics_path,
                {
                    "update": update_idx,
                    "global_step": global_step,
                    "benchmark": _strip_episode_details(eval_report["benchmark"]),
                    "stochastic": {
                        name: _strip_episode_details(payload)
                        for name, payload in eval_report["stochastic"].items()
                    },
                },
            )
            _write_json(
                path=benchmark_report_path,
                payload=eval_report["benchmark"],
            )
            _write_json(
                path=stochastic_report_path,
                payload=eval_report["stochastic_report"],
            )

        if update_idx % train_cfg.save_interval_updates == 0 or global_step >= train_cfg.total_timesteps:
            _save_policy_checkpoint(
                path=policy_path,
                model=model,
                optimizer=optimizer,
                critic_schema_hash=critic_schema.schema_hash,
                model_version=model_version,
                global_step=global_step,
                update_idx=update_idx,
            )

    _save_policy_checkpoint(
        path=policy_path,
        model=model,
        optimizer=optimizer,
        critic_schema_hash=critic_schema.schema_hash,
        model_version=model_version,
        global_step=global_step,
        update_idx=update_idx,
    )
    meta_payload = _build_meta_payload(
        scene_ctx=scene_ctx,
        order_source=order_source,
        critic_schema=critic_schema,
        config_path=Path(config_path),
        model_version=model_version,
    )
    _validate_meta_payload_before_write(
        meta_payload=meta_payload,
        scene_ctx=scene_ctx,
        order_source=order_source,
        critic_schema=critic_schema,
        train_cfg=train_cfg,
        model_version=model_version,
        global_step=global_step,
        policy_path=policy_path,
        config_snapshot_path=config_snapshot_path,
        metrics_path=metrics_path,
    )
    _write_json(path=meta_path, payload=meta_payload)

    return {
        "model_version": model_version,
        "global_step": global_step,
        "updates": update_idx,
        "policy_path": str(policy_path),
        "meta_path": str(meta_path),
        "metrics_path": str(metrics_path),
        "episode_metrics_path": str(episode_metrics_path),
        "eval_metrics_path": str(eval_metrics_path),
        "benchmark_report_path": str(benchmark_report_path),
        "stochastic_report_path": str(stochastic_report_path),
    }


def _record_episode_step(accumulator: _EpisodeAccumulator, *, reward: float) -> None:
    accumulator.decision_count += 1
    accumulator.total_reward += float(reward)


def _finalize_episode_metrics(
    *,
    accumulator: _EpisodeAccumulator,
    episode_snapshot: Mapping[str, Any],
    global_step_end: int | None,
    update_idx_end: int | None,
) -> dict[str, Any]:
    payload = {
        "phase": accumulator.phase,
        "episode_id": int(accumulator.episode_id),
        "order_source_mode": str(accumulator.order_source_mode),
        "order_source_seed": int(accumulator.order_source_seed),
        "total_reward": float(accumulator.total_reward),
        "episode_length_decisions": int(accumulator.decision_count),
        "global_step_start": accumulator.global_step_start,
        "global_step_end": global_step_end,
        "update_start": accumulator.update_start,
        "update_end": update_idx_end,
    }
    payload.update(dict(episode_snapshot))
    return payload


def _run_periodic_evaluation(
    *,
    scene_ctx: Any,
    config_path: Path,
    model: Any,
    tensorizer: ObservationTensorizer,
    critic_builder: CriticBatchBuilder,
    critic_schema: CriticTensorSchemaMeta,
    policy_cfg: _PolicyConfig,
    eval_cfg: _EvalConfig,
    train_cfg: _TrainingConfig,
    device: Any,
    update_idx: int,
    global_step: int,
) -> dict[str, Any]:
    rng_state = _capture_rng_state()
    was_training = bool(model.training)
    model.eval()
    try:
        generated_at = datetime.now(timezone.utc).isoformat()
        benchmark_episodes = [
            _evaluate_policy_episode(
                scene_ctx=scene_ctx,
                config_path=config_path,
                order_source=build_order_source(
                    scene_ctx,
                    mode=OrderSourceMode.BENCHMARK,
                    seed=eval_cfg.benchmark_seed,
                    config_path=config_path,
                ),
                model=model,
                tensorizer=tensorizer,
                critic_builder=critic_builder,
                critic_schema=critic_schema,
                policy_cfg=policy_cfg,
                device=device,
                eval_phase="benchmark",
                episode_id=episode_idx,
            )
            for episode_idx in range(train_cfg.benchmark_eval_episodes)
        ]
        benchmark_report = _summarize_episode_records(
            split="benchmark",
            order_source_mode=OrderSourceMode.BENCHMARK.value,
            episodes=benchmark_episodes,
            update_idx=update_idx,
            global_step=global_step,
            generated_at=generated_at,
            extra={
                "benchmark_seed": int(eval_cfg.benchmark_seed),
                "benchmark_eval_episodes": int(train_cfg.benchmark_eval_episodes),
            },
        )

        stochastic_reports: dict[str, dict[str, Any]] = {}
        for band_name, arrival_rate in eval_cfg.stochastic_arrival_rates:
            band_episodes = []
            for seed_offset in range(train_cfg.stochastic_eval_seeds):
                order_source = build_order_source(
                    scene_ctx,
                    mode=OrderSourceMode.POISSON,
                    seed=eval_cfg.stochastic_seed_base + seed_offset,
                    overrides={"poisson_arrival_rate": arrival_rate},
                    config_path=config_path,
                )
                band_episodes.append(
                    _evaluate_policy_episode(
                        scene_ctx=scene_ctx,
                        config_path=config_path,
                        order_source=order_source,
                        model=model,
                        tensorizer=tensorizer,
                        critic_builder=critic_builder,
                        critic_schema=critic_schema,
                        policy_cfg=policy_cfg,
                        device=device,
                        eval_phase=f"stochastic_{band_name}",
                        episode_id=seed_offset,
                    )
                )
            stochastic_reports[band_name] = _summarize_episode_records(
                split=f"stochastic_{band_name}",
                order_source_mode=OrderSourceMode.POISSON.value,
                episodes=band_episodes,
                update_idx=update_idx,
                global_step=global_step,
                generated_at=generated_at,
                extra={
                    "arrival_rate_per_min": float(arrival_rate),
                    "seed_base": int(eval_cfg.stochastic_seed_base),
                    "seed_count": int(train_cfg.stochastic_eval_seeds),
                },
            )

        return {
            "benchmark": benchmark_report,
            "stochastic": stochastic_reports,
            "stochastic_report": {
                "generated_at": generated_at,
                "update": int(update_idx),
                "global_step": int(global_step),
                "order_source_mode": OrderSourceMode.POISSON.value,
                "profiles": stochastic_reports,
            },
        }
    finally:
        _restore_rng_state(rng_state)
        if was_training:
            model.train()


def _evaluate_policy_episode(
    *,
    scene_ctx: Any,
    config_path: Path,
    order_source: Any,
    model: Any,
    tensorizer: ObservationTensorizer,
    critic_builder: CriticBatchBuilder,
    critic_schema: CriticTensorSchemaMeta,
    policy_cfg: _PolicyConfig,
    device: Any,
    eval_phase: str,
    episode_id: int,
) -> dict[str, Any]:
    env = TrainingEnvAdapter(
        scene_ctx=scene_ctx,
        order_source=order_source,
        config_path=config_path,
    )
    result = env.reset()
    if result.decision_context is None:
        raise RuntimeError("eval reset 后没有可用 decision_context")

    last_seen_plan_version_by_drone: dict[str, int] = {}
    history_buffer: deque[Any] = deque(maxlen=policy_cfg.hist_len)
    lstm_state_by_drone: dict[str, Any] = {}
    recurrent_segment_id_by_drone: dict[str, int] = {}
    episode_acc = _EpisodeAccumulator(
        phase=eval_phase,
        episode_id=episode_id,
        order_source_mode=str(order_source.mode.value),
        order_source_seed=int(order_source.seed),
    )

    while not result.done:
        decision_context = result.decision_context
        if decision_context is None:
            raise RuntimeError("eval 过程中 decision_context 丢失")

        drone_id = str(decision_context.deciding_drone_id)
        candidate_out = _build_candidate_output(
            env=env,
            decision_context=decision_context,
            last_seen_plan_version_by_drone=last_seen_plan_version_by_drone,
        )
        observation_batch = tensorizer.build(
            decision_context=decision_context,
            candidate_out=candidate_out,
            transition_history=tuple(history_buffer),
        )
        action_mask = tensorizer.build_action_mask(candidate_out)
        critic_batch = critic_builder.build(
            decision_context=decision_context,
            critic_tensor_schema_meta=critic_schema,
        )
        with torch.no_grad():
            policy_out, next_lstm_state = model.forward(
                observation_batch=observation_batch,
                action_mask=action_mask,
                critic_batch=critic_batch,
                lstm_state=lstm_state_by_drone.get(drone_id),
            )
            sampled_action, _ = model.sample_action(
                policy_out=policy_out,
                action_mask=action_mask,
                deterministic=True,
            )
        action_indices = ResolvedActionIndices(**sampled_action)
        env_action = candidate_out.resolved_action_lookup.resolve(
            root_branch_idx=action_indices.root_branch_idx,
            order_idx=action_indices.order_idx,
            mode_idx=action_indices.mode_idx,
            recovery_idx=action_indices.recovery_idx,
        )
        step_result = env.step(env_action)
        history_buffer.append(
            tensorizer.build_transition_summary(
                decision_context=decision_context,
                candidate_out=candidate_out,
                action_indices=action_indices,
                step_result=step_result,
            )
        )
        last_seen_plan_version_by_drone[decision_context.deciding_drone_id] = (
            decision_context.coarse_plan.plan_version
        )
        lstm_state_by_drone[drone_id] = _detach_lstm_state(lstm_state=next_lstm_state)
        _reset_recurrent_state_for_failed_drones(
            runtime_state=step_result.runtime_state,
            lstm_state_by_drone=lstm_state_by_drone,
            recurrent_segment_id_by_drone=recurrent_segment_id_by_drone,
        )
        _record_episode_step(episode_acc, reward=float(step_result.reward))
        result = step_result

    return _finalize_episode_metrics(
        accumulator=episode_acc,
        episode_snapshot=env.build_episode_metrics_snapshot(),
        global_step_end=None,
        update_idx_end=None,
    )


def _summarize_episode_records(
    *,
    split: str,
    order_source_mode: str,
    episodes: Sequence[Mapping[str, Any]],
    update_idx: int,
    global_step: int,
    generated_at: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not episodes:
        raise ValueError(f"{split} 评估结果不能为空")

    episode_count = len(episodes)
    mode_b_total = float(sum(float(item["dispatch_mode_b_count"]) for item in episodes))
    mode_c_total = float(sum(float(item["dispatch_mode_c_count"]) for item in episodes))
    dispatch_total = mode_b_total + mode_c_total
    payload = {
        "generated_at": generated_at,
        "update": int(update_idx),
        "global_step": int(global_step),
        "split": str(split),
        "order_source_mode": str(order_source_mode),
        "episode_count": int(episode_count),
        "mean_total_reward": float(np.mean([float(item["total_reward"]) for item in episodes])),
        "mean_episode_length_decisions": float(
            np.mean([float(item["episode_length_decisions"]) for item in episodes])
        ),
        "mean_episode_end_t_sec": float(
            np.mean([float(item["episode_end_t_sec"]) for item in episodes])
        ),
        "mean_delivery_count": float(np.mean([float(item["delivery_count"]) for item in episodes])),
        "sum_delivery_count": int(sum(int(item["delivery_count"]) for item in episodes)),
        "mean_on_time_rate": float(np.mean([float(item["on_time_rate"]) for item in episodes])),
        "sum_timeout_order_count": int(sum(int(item["timeout_order_count"]) for item in episodes)),
        "sum_fallback_count": int(sum(int(item["fallback_count"]) for item in episodes)),
        "sum_hard_failure_count": int(sum(int(item["hard_failure_count"]) for item in episodes)),
        "sum_reservation_timeout_count": int(
            sum(int(item["reservation_timeout_count"]) for item in episodes)
        ),
        "sum_hard_overdue_count": int(
            sum(int(item["hard_overdue_count"]) for item in episodes)
        ),
        "sum_t_wait_sec": float(sum(float(item["t_wait_sec"]) for item in episodes)),
        "sum_t_idle_sec": float(sum(float(item["t_idle_sec"]) for item in episodes)),
        "sum_t_queue_sec": float(sum(float(item["t_queue_sec"]) for item in episodes)),
        "sum_t_fallback_sec": float(sum(float(item["t_fallback_sec"]) for item in episodes)),
        "sum_t_overdue_sec": float(sum(float(item["t_overdue_sec"]) for item in episodes)),
        "sum_t_reservation_timeout_cost_sec": float(
            sum(float(item["t_reservation_timeout_cost_sec"]) for item in episodes)
        ),
        "mode_b_dispatch_ratio": (
            float(mode_b_total / dispatch_total) if dispatch_total > 0.0 else 0.0
        ),
        "mode_c_dispatch_ratio": (
            float(mode_c_total / dispatch_total) if dispatch_total > 0.0 else 0.0
        ),
        "episodes": [dict(item) for item in episodes],
    }
    if extra:
        payload.update(dict(extra))
    return payload


def _strip_episode_details(report: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload.pop("episodes", None)
    return payload


def _capture_rng_state() -> dict[str, Any]:
    state = {
        "python": random.getstate(),
        "numpy": np.random.get_state(),
        "torch_cpu": torch.random.get_rng_state(),
    }
    if torch.cuda.is_available():
        state["torch_cuda"] = torch.cuda.get_rng_state_all()
    return state


def _restore_rng_state(state: Mapping[str, Any]) -> None:
    random.setstate(state["python"])
    np.random.set_state(state["numpy"])
    torch.random.set_rng_state(state["torch_cpu"])
    if "torch_cuda" in state and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(state["torch_cuda"])


def _ppo_update(
    *,
    model: Any,
    optimizer: Any,
    batch_view: Any,
    train_cfg: _TrainingConfig,
    device: Any,
) -> dict[str, float]:
    sequences = _materialize_recurrent_sequences(
        batch_view=batch_view,
        sequence_len=train_cfg.sequence_len,
    )
    if not sequences:
        raise RuntimeError("当前 rollout 未物化出任何 recurrent sequence")

    total_valid_timesteps = int(
        sum(int(sequence.valid_timestep_mask.sum()) for sequence in sequences)
    )
    total_padded_timesteps = int(
        sum(train_cfg.sequence_len - len(sequence.transitions) for sequence in sequences)
    )
    latest_approx_kl = 0.0
    latest_policy_loss = 0.0
    latest_value_loss = 0.0
    latest_entropy = 0.0
    sequence_indices = np.arange(len(sequences), dtype=np.int64)

    stop_early = False
    for _epoch in range(train_cfg.ppo_epochs):
        np.random.shuffle(sequence_indices)
        for start in range(0, len(sequence_indices), train_cfg.sequence_minibatch_size):
            picked = sequence_indices[start : start + train_cfg.sequence_minibatch_size]
            minibatch = _build_sequence_minibatch(
                sequences=[sequences[int(idx)] for idx in picked],
                train_cfg=train_cfg,
                model=model,
                device=device,
            )
            valid_mask = minibatch.valid_timestep_mask
            advantages_t = minibatch.advantages
            if train_cfg.normalize_advantage:
                advantages_t = _normalize_advantages_masked(
                    advantages=advantages_t,
                    valid_mask=valid_mask,
                )

            policy_seq_out, _ = model.forward_sequence(
                observation_batch=minibatch.observation_batch,
                action_mask=minibatch.action_mask,
                critic_batch=minibatch.critic_batch,
                lstm_state=minibatch.lstm_state_in,
            )
            flat_policy_out = _flatten_sequence_policy_output(policy_seq_out)
            flat_action_mask = _flatten_sequence_action_mask(minibatch.action_mask)
            flat_action_indices = _flatten_sequence_action_indices(minibatch.action_indices)
            new_log_probs, entropy = model.evaluate_actions(
                policy_out=flat_policy_out,
                action_mask=flat_action_mask,
                action_indices=flat_action_indices,
            )

            old_log_probs = minibatch.old_log_probs.reshape(-1)
            old_values_flat = minibatch.old_values.reshape(-1)
            returns = minibatch.returns.reshape(-1)
            advantages_flat = advantages_t.reshape(-1)
            valid_mask_flat = valid_mask.reshape(-1)
            values_flat = policy_seq_out.value.reshape(-1)

            ratio = torch.exp(new_log_probs - old_log_probs)
            unclipped = ratio * advantages_flat
            clipped = torch.clamp(
                ratio,
                1.0 - train_cfg.clip_coef,
                1.0 + train_cfg.clip_coef,
            ) * advantages_flat
            policy_loss = -_masked_mean_tensor(
                torch.min(unclipped, clipped),
                valid_mask_flat,
            )
            v_clipped = old_values_flat + torch.clamp(
                values_flat - old_values_flat,
                -train_cfg.vf_clip_coef,
                train_cfg.vf_clip_coef,
            )
            vl_unclipped = (values_flat - returns) ** 2
            vl_clipped = (v_clipped - returns) ** 2
            value_loss = 0.5 * _masked_mean_tensor(
                torch.max(vl_unclipped, vl_clipped),
                valid_mask_flat,
            )
            entropy_loss = _masked_mean_tensor(entropy, valid_mask_flat)
            loss = (
                policy_loss
                + train_cfg.value_loss_coef * value_loss
                - train_cfg.entropy_coef * entropy_loss
            )

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), train_cfg.max_grad_norm)
            optimizer.step()

            latest_approx_kl = float(
                _masked_mean_tensor(
                    (old_log_probs - new_log_probs).abs(),
                    valid_mask_flat,
                ).detach().cpu().item()
            )
            latest_policy_loss = float(policy_loss.detach().cpu().item())
            latest_value_loss = float(value_loss.detach().cpu().item())
            latest_entropy = float(entropy_loss.detach().cpu().item())
            if latest_approx_kl > train_cfg.target_kl:
                stop_early = True
                break
        if stop_early:
            break

    return {
        "policy_loss": latest_policy_loss,
        "value_loss": latest_value_loss,
        "entropy": latest_entropy,
        "approx_kl": latest_approx_kl,
        "adv_mean": float(
            np.mean(
                np.concatenate(
                    [sequence.advantages[sequence.valid_timestep_mask] for sequence in sequences]
                )
            )
        ),
        "return_mean": float(np.mean(batch_view.returns)),
        "reward_mean": float(np.mean(batch_view.rewards)),
        "sequence_count": float(len(sequences)),
        "valid_timestep_count": float(total_valid_timesteps),
        "padded_timestep_count": float(total_padded_timesteps),
    }


def _build_candidate_output(
    *,
    env: TrainingEnvAdapter,
    decision_context: Any,
    last_seen_plan_version_by_drone: Mapping[str, int],
) -> Any:
    drone_id = str(decision_context.deciding_drone_id)
    last_seen = last_seen_plan_version_by_drone.get(
        drone_id,
        int(decision_context.coarse_plan.plan_version),
    )
    return env.build_candidate_output(
        decision_context,
        last_seen_plan_version=last_seen,
    )


def _detach_lstm_state(
    *,
    lstm_state: Any | None,
) -> Any | None:
    if lstm_state is None:
        return None
    hidden, cell = lstm_state
    return hidden.detach().cpu().clone(), cell.detach().cpu().clone()


def _materialize_recurrent_sequences(
    *,
    batch_view: Any,
    sequence_len: int,
) -> list[_MaterializedSequence]:
    grouped_indices: dict[tuple[int, str, int], list[int]] = {}
    for idx, transition in enumerate(batch_view.transitions):
        key = (
            int(transition.episode_id),
            str(transition.actor_drone_id),
            int(transition.recurrent_segment_id),
        )
        grouped_indices.setdefault(key, []).append(idx)

    sequences: list[_MaterializedSequence] = []
    for key, indices in grouped_indices.items():
        indices.sort(
            key=lambda item_idx: int(batch_view.transitions[item_idx].local_decision_index)
        )
        for local_chunk_idx, start in enumerate(range(0, len(indices), sequence_len)):
            chunk_indices = indices[start : start + sequence_len]
            transitions = tuple(batch_view.transitions[item_idx] for item_idx in chunk_indices)
            advantages = np.asarray(
                [batch_view.advantages[item_idx] for item_idx in chunk_indices],
                dtype=np.float32,
            )
            returns = np.asarray(
                [batch_view.returns[item_idx] for item_idx in chunk_indices],
                dtype=np.float32,
            )
            valid_mask = np.ones((len(chunk_indices),), dtype=np.bool_)
            sequences.append(
                _MaterializedSequence(
                    sequence_id=(key[0], key[1], key[2], local_chunk_idx),
                    episode_id=key[0],
                    actor_drone_id=key[1],
                    recurrent_segment_id=key[2],
                    local_chunk_idx=local_chunk_idx,
                    transitions=transitions,
                    advantages=advantages,
                    returns=returns,
                    valid_timestep_mask=valid_mask,
                )
            )
    sequences.sort(
        key=lambda item: (
            item.episode_id,
            min(
                transition.local_decision_index for transition in item.transitions
            ),
            item.actor_drone_id,
            item.recurrent_segment_id,
            item.local_chunk_idx,
        )
    )
    return sequences


def _stack_lstm_states_for_update(
    transitions: Sequence[RolloutTransition],
    *,
    model: Any,
    device: Any,
) -> Any | None:
    if not transitions:
        return None

    num_layers = int(model.recurrent_core.num_layers)
    hidden_size = int(model.recurrent_core.hidden_size)
    has_any_state = any(item.lstm_state_in is not None for item in transitions)
    if not has_any_state:
        return None

    hidden_parts = []
    cell_parts = []
    for idx, transition in enumerate(transitions):
        state = transition.lstm_state_in
        if state is None:
            hidden_parts.append(torch.zeros((num_layers, hidden_size), dtype=torch.float32, device=device))
            cell_parts.append(torch.zeros((num_layers, hidden_size), dtype=torch.float32, device=device))
            continue

        hidden, cell = state
        hidden = torch.as_tensor(hidden, dtype=torch.float32, device=device)
        cell = torch.as_tensor(cell, dtype=torch.float32, device=device)
        expected_shape = (num_layers, 1, hidden_size)
        if tuple(hidden.shape) != expected_shape or tuple(cell.shape) != expected_shape:
            raise ValueError(
                "rollout 中保存的 lstm_state_in 形状不合法: "
                f"idx={idx}, hidden={tuple(hidden.shape)}, cell={tuple(cell.shape)}, "
                f"expected={expected_shape}"
            )
        hidden_parts.append(hidden.squeeze(1))
        cell_parts.append(cell.squeeze(1))

    return torch.stack(hidden_parts, dim=1), torch.stack(cell_parts, dim=1)


def _build_sequence_minibatch(
    *,
    sequences: Sequence[_MaterializedSequence],
    train_cfg: _TrainingConfig,
    model: Any,
    device: Any,
) -> _SequenceMiniBatch:
    if not sequences:
        raise ValueError("sequence minibatch 不能为空")

    template_transition = sequences[0].transitions[0]
    pad_observation = _zero_observation_batch_like(template_transition.observation_batch)
    pad_critic = _zero_critic_batch_like(template_transition.critic_batch)
    pad_action_mask = _zero_action_mask_like(template_transition.action_mask)

    batch_size = len(sequences)
    seq_len = train_cfg.sequence_len

    observation_rows = []
    critic_rows = []
    action_mask_rows = []
    root_branch_idx = np.zeros((batch_size, seq_len), dtype=np.int64)
    order_idx = np.zeros((batch_size, seq_len), dtype=np.int64)
    mode_idx = np.zeros((batch_size, seq_len), dtype=np.int64)
    recovery_idx = np.zeros((batch_size, seq_len), dtype=np.int64)
    old_log_probs = np.zeros((batch_size, seq_len), dtype=np.float32)
    old_values = np.zeros((batch_size, seq_len), dtype=np.float32)
    returns = np.zeros((batch_size, seq_len), dtype=np.float32)
    advantages = np.zeros((batch_size, seq_len), dtype=np.float32)
    valid_timestep_mask = np.zeros((batch_size, seq_len), dtype=np.bool_)
    padded_timesteps = 0

    for seq_row, sequence in enumerate(sequences):
        step_transitions = list(sequence.transitions)
        pad_count = seq_len - len(step_transitions)
        padded_timesteps += pad_count

        obs_steps = [item.observation_batch for item in step_transitions]
        critic_steps = [item.critic_batch for item in step_transitions]
        mask_steps = [item.action_mask for item in step_transitions]
        if pad_count > 0:
            obs_steps.extend([pad_observation] * pad_count)
            critic_steps.extend([pad_critic] * pad_count)
            mask_steps.extend([pad_action_mask] * pad_count)

        observation_rows.append(_stack_observation_batches_from_steps(obs_steps))
        critic_rows.append(_stack_critic_batches_from_steps(critic_steps))
        action_mask_rows.append(_stack_action_masks_from_steps(mask_steps))

        for step_idx, transition in enumerate(step_transitions):
            action = transition.action_indices
            root_branch_idx[seq_row, step_idx] = int(action.root_branch_idx)
            order_idx[seq_row, step_idx] = 0 if action.order_idx is None else int(action.order_idx)
            mode_idx[seq_row, step_idx] = 0 if action.mode_idx is None else int(action.mode_idx)
            recovery_idx[seq_row, step_idx] = (
                0 if action.recovery_idx is None else int(action.recovery_idx)
            )
            old_log_probs[seq_row, step_idx] = float(transition.log_prob_old)
            old_values[seq_row, step_idx] = float(transition.value_old)
            returns[seq_row, step_idx] = float(sequence.returns[step_idx])
            advantages[seq_row, step_idx] = float(sequence.advantages[step_idx])
            valid_timestep_mask[seq_row, step_idx] = bool(sequence.valid_timestep_mask[step_idx])

    observation_batch = _stack_sequence_observation_rows(observation_rows)
    critic_batch = _stack_sequence_critic_rows(critic_rows)
    action_mask = _stack_sequence_action_mask_rows(action_mask_rows)
    first_transitions = tuple(sequence.transitions[0] for sequence in sequences)
    lstm_state_in = _stack_lstm_states_for_update(
        first_transitions,
        model=model,
        device=device,
    )

    return _SequenceMiniBatch(
        observation_batch=observation_batch,
        critic_batch=critic_batch,
        action_mask=action_mask,
        action_indices={
            "root_branch_idx": torch.as_tensor(root_branch_idx, dtype=torch.long, device=device),
            "order_idx": torch.as_tensor(order_idx, dtype=torch.long, device=device),
            "mode_idx": torch.as_tensor(mode_idx, dtype=torch.long, device=device),
            "recovery_idx": torch.as_tensor(recovery_idx, dtype=torch.long, device=device),
        },
        old_log_probs=torch.as_tensor(old_log_probs, dtype=torch.float32, device=device),
        old_values=torch.as_tensor(old_values, dtype=torch.float32, device=device),
        returns=torch.as_tensor(returns, dtype=torch.float32, device=device),
        advantages=torch.as_tensor(advantages, dtype=torch.float32, device=device),
        valid_timestep_mask=torch.as_tensor(valid_timestep_mask, dtype=torch.bool, device=device),
        lstm_state_in=lstm_state_in,
        sequence_count=batch_size,
        padded_timesteps=padded_timesteps,
    )


def _stack_observation_batches_from_steps(step_batches: Sequence[Any]) -> Any:
    cls = step_batches[0].__class__
    return cls(
        uav_self_token=np.stack([item.uav_self_token for item in step_batches], axis=0),
        order_tokens=np.stack([item.order_tokens for item in step_batches], axis=0),
        recovery_tokens=np.stack([item.recovery_tokens for item in step_batches], axis=0),
        infra_tokens=np.stack([item.infra_tokens for item in step_batches], axis=0),
        history_tokens=np.stack([item.history_tokens for item in step_batches], axis=0),
        history_padding_mask=np.stack([item.history_padding_mask for item in step_batches], axis=0),
        padding_mask=np.stack([item.padding_mask for item in step_batches], axis=0),
        recovery_padding_mask=np.stack([item.recovery_padding_mask for item in step_batches], axis=0),
    )


def _stack_critic_batches_from_steps(step_batches: Sequence[Any]) -> Any:
    cls = step_batches[0].__class__
    return cls(
        global_order_pool_tokens=np.stack([item.global_order_pool_tokens for item in step_batches], axis=0),
        global_uav_tokens=np.stack([item.global_uav_tokens for item in step_batches], axis=0),
        global_station_tokens=np.stack([item.global_station_tokens for item in step_batches], axis=0),
        coarse_plan_summary_vec=np.stack([item.coarse_plan_summary_vec for item in step_batches], axis=0),
        global_system_summary_vec=np.stack([item.global_system_summary_vec for item in step_batches], axis=0),
        global_order_padding_mask=np.stack([item.global_order_padding_mask for item in step_batches], axis=0),
        global_uav_padding_mask=np.stack([item.global_uav_padding_mask for item in step_batches], axis=0),
        global_station_padding_mask=np.stack([item.global_station_padding_mask for item in step_batches], axis=0),
    )


def _stack_action_masks_from_steps(step_masks: Sequence[Any]) -> Any:
    cls = step_masks[0].__class__
    return cls(
        root_branch_mask=np.stack([item.root_branch_mask for item in step_masks], axis=0),
        order_mask=np.stack([item.order_mask for item in step_masks], axis=0),
        mode_mask=np.stack([item.mode_mask for item in step_masks], axis=0),
        recovery_mask=np.stack([item.recovery_mask for item in step_masks], axis=0),
    )


def _stack_sequence_observation_rows(rows: Sequence[Any]) -> Any:
    cls = rows[0].__class__
    return cls(
        uav_self_token=np.stack([item.uav_self_token for item in rows], axis=0),
        order_tokens=np.stack([item.order_tokens for item in rows], axis=0),
        recovery_tokens=np.stack([item.recovery_tokens for item in rows], axis=0),
        infra_tokens=np.stack([item.infra_tokens for item in rows], axis=0),
        history_tokens=np.stack([item.history_tokens for item in rows], axis=0),
        history_padding_mask=np.stack([item.history_padding_mask for item in rows], axis=0),
        padding_mask=np.stack([item.padding_mask for item in rows], axis=0),
        recovery_padding_mask=np.stack([item.recovery_padding_mask for item in rows], axis=0),
    )


def _stack_sequence_critic_rows(rows: Sequence[Any]) -> Any:
    cls = rows[0].__class__
    return cls(
        global_order_pool_tokens=np.stack([item.global_order_pool_tokens for item in rows], axis=0),
        global_uav_tokens=np.stack([item.global_uav_tokens for item in rows], axis=0),
        global_station_tokens=np.stack([item.global_station_tokens for item in rows], axis=0),
        coarse_plan_summary_vec=np.stack([item.coarse_plan_summary_vec for item in rows], axis=0),
        global_system_summary_vec=np.stack([item.global_system_summary_vec for item in rows], axis=0),
        global_order_padding_mask=np.stack([item.global_order_padding_mask for item in rows], axis=0),
        global_uav_padding_mask=np.stack([item.global_uav_padding_mask for item in rows], axis=0),
        global_station_padding_mask=np.stack([item.global_station_padding_mask for item in rows], axis=0),
    )


def _stack_sequence_action_mask_rows(rows: Sequence[Any]) -> Any:
    cls = rows[0].__class__
    return cls(
        root_branch_mask=np.stack([item.root_branch_mask for item in rows], axis=0),
        order_mask=np.stack([item.order_mask for item in rows], axis=0),
        mode_mask=np.stack([item.mode_mask for item in rows], axis=0),
        recovery_mask=np.stack([item.recovery_mask for item in rows], axis=0),
    )


def _zero_observation_batch_like(template: Any) -> Any:
    cls = template.__class__
    return cls(
        uav_self_token=np.zeros_like(template.uav_self_token),
        order_tokens=np.zeros_like(template.order_tokens),
        recovery_tokens=np.zeros_like(template.recovery_tokens),
        infra_tokens=np.zeros_like(template.infra_tokens),
        history_tokens=np.zeros_like(template.history_tokens),
        history_padding_mask=np.ones_like(template.history_padding_mask, dtype=np.bool_),
        padding_mask=np.ones_like(template.padding_mask, dtype=np.bool_),
        recovery_padding_mask=np.ones_like(template.recovery_padding_mask, dtype=np.bool_),
    )


def _zero_critic_batch_like(template: Any) -> Any:
    cls = template.__class__
    return cls(
        global_order_pool_tokens=np.zeros_like(template.global_order_pool_tokens),
        global_uav_tokens=np.zeros_like(template.global_uav_tokens),
        global_station_tokens=np.zeros_like(template.global_station_tokens),
        coarse_plan_summary_vec=np.zeros_like(template.coarse_plan_summary_vec),
        global_system_summary_vec=np.zeros_like(template.global_system_summary_vec),
        global_order_padding_mask=np.ones_like(template.global_order_padding_mask, dtype=np.bool_),
        global_uav_padding_mask=np.ones_like(template.global_uav_padding_mask, dtype=np.bool_),
        global_station_padding_mask=np.ones_like(template.global_station_padding_mask, dtype=np.bool_),
    )


def _zero_action_mask_like(template: Any) -> Any:
    cls = template.__class__
    root_branch_mask = np.zeros_like(template.root_branch_mask, dtype=np.bool_)
    if root_branch_mask.size != 2:
        raise ValueError(f"root_branch_mask 形状异常，无法构造 WAIT-only padding: {root_branch_mask.shape}")
    root_branch_mask[0] = True
    return cls(
        root_branch_mask=root_branch_mask,
        order_mask=np.zeros_like(template.order_mask, dtype=np.bool_),
        mode_mask=np.zeros_like(template.mode_mask, dtype=np.bool_),
        recovery_mask=np.zeros_like(template.recovery_mask, dtype=np.bool_),
    )


def _flatten_sequence_policy_output(policy_out: Any) -> Any:
    cls = policy_out.__class__
    return cls(
        root_branch_logits=policy_out.root_branch_logits.reshape(-1, policy_out.root_branch_logits.shape[-1]),
        order_logits=policy_out.order_logits.reshape(-1, policy_out.order_logits.shape[-1]),
        mode_logits=policy_out.mode_logits.reshape(
            -1,
            policy_out.mode_logits.shape[-2],
            policy_out.mode_logits.shape[-1],
        ),
        recovery_logits=policy_out.recovery_logits.reshape(
            -1,
            policy_out.recovery_logits.shape[-2],
            policy_out.recovery_logits.shape[-1],
        ),
        value=policy_out.value.reshape(-1),
    )


def _flatten_sequence_action_mask(action_mask: Any) -> Any:
    cls = action_mask.__class__
    return cls(
        root_branch_mask=action_mask.root_branch_mask.reshape(-1, action_mask.root_branch_mask.shape[-1]),
        order_mask=action_mask.order_mask.reshape(-1, action_mask.order_mask.shape[-1]),
        mode_mask=action_mask.mode_mask.reshape(
            -1,
            action_mask.mode_mask.shape[-2],
            action_mask.mode_mask.shape[-1],
        ),
        recovery_mask=action_mask.recovery_mask.reshape(
            -1,
            action_mask.recovery_mask.shape[-2],
            action_mask.recovery_mask.shape[-1],
        ),
    )


def _flatten_sequence_action_indices(action_indices: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "root_branch_idx": action_indices["root_branch_idx"].reshape(-1),
        "order_idx": action_indices["order_idx"].reshape(-1),
        "mode_idx": action_indices["mode_idx"].reshape(-1),
        "recovery_idx": action_indices["recovery_idx"].reshape(-1),
    }


def _masked_mean_tensor(values: Any, valid_mask: Any) -> Any:
    weights = valid_mask.to(dtype=values.dtype)
    denom = weights.sum().clamp(min=1.0)
    return (values * weights).sum() / denom


def _normalize_advantages_masked(*, advantages: Any, valid_mask: Any) -> Any:
    weights = valid_mask.to(dtype=advantages.dtype)
    valid_count = weights.sum().clamp(min=1.0)
    mean = (advantages * weights).sum() / valid_count
    centered = (advantages - mean) * weights
    variance = (centered * centered).sum() / valid_count
    normalized = centered / torch.sqrt(variance.clamp(min=1e-8))
    return normalized * weights


def _reset_recurrent_state_for_failed_drones(
    *,
    runtime_state: Any,
    lstm_state_by_drone: dict[str, Any],
    recurrent_segment_id_by_drone: dict[str, int],
) -> None:
    for drone_id, drone_state in runtime_state.drone_states.items():
        if str(drone_state.training_state) != "airborne_energy_failure":
            continue
        if drone_id not in lstm_state_by_drone:
            continue
        lstm_state_by_drone.pop(drone_id, None)
        recurrent_segment_id_by_drone[drone_id] = int(
            recurrent_segment_id_by_drone.get(drone_id, 0)
        ) + 1


def _build_meta_payload(
    *,
    scene_ctx: Any,
    order_source: Any,
    critic_schema: CriticTensorSchemaMeta,
    config_path: Path,
    model_version: str,
) -> dict[str, Any]:
    raw = _load_yaml(config_path)
    action_space = raw["action_space"]
    candidate = raw["candidate"]
    planner = raw["planner"]
    policy = raw["policy"]
    reward = raw["reward"]
    reservation = raw["reservation"]

    params = load_drone_params()
    meta = MetaJson(
        schema_version="phase7_v1",
        coarse_plan_view_contract_version="v1",
        env_semantic_contract_version="v1",
        policy=PolicyMeta(
            encoder_type=str(policy["encoder_type"]),
            d_model=int(policy["d_model"]),
            nhead=int(policy["nhead"]),
            ff_dim=int(policy["ff_dim"]),
            dropout=float(policy["dropout"]),
            lstm_hidden=int(policy["lstm_hidden"]),
            lstm_layers=int(policy["lstm_layers"]),
            hist_len=int(policy["hist_len"]),
            max_order_tokens=int(policy["max_order_tokens"]),
            max_recovery_tokens=int(policy["max_recovery_tokens"]),
            use_plan_version_delta=bool(policy["use_plan_version_delta"]),
            use_is_riding_truck_flag=bool(policy["use_is_riding_truck_flag"]),
            use_drone_source_type_flag=bool(policy["use_drone_source_type_flag"]),
            critic_mode=str(policy["critic_mode"]),
            inference_mode=str(policy["inference_mode"]),
        ),
        action_space=ActionSpaceMeta(
            type=str(action_space["type"]),
            factorized_head_order=tuple(action_space["factorized_head_order"]),
            policy_modes=tuple(action_space["policy_modes"]),
            planner_modes=tuple(action_space["planner_modes"]),
            enable_wait_action=bool(action_space["enable_wait_action"]),
            include_mode_a_in_policy=bool(action_space["include_mode_a_in_policy"]),
        ),
        candidate=CandidateMeta(
            max_candidate_orders=int(candidate["max_candidate_orders"]),
            max_candidate_recovery_per_order=int(candidate["max_candidate_recovery_per_order"]),
            max_candidate_actions=int(candidate["max_candidate_actions"]),
            station_wait_threshold_sec=float(candidate["station_wait_threshold_sec"]),
            rendezvous_eta_safe_margin_sec=float(candidate["rendezvous_eta_safe_margin_sec"]),
            energy_safe_margin_ratio=float(candidate["energy_safe_margin_ratio"]),
        ),
        planner=PlannerMeta(
            coarse_replan_interval_sec=float(planner["coarse_replan_interval_sec"]),
            coarse_new_order_trigger=int(planner["coarse_new_order_trigger"]),
            route_drift_trigger_ratio=float(planner["route_drift_trigger_ratio"]),
            fallback_burst_trigger_count=int(planner["fallback_burst_trigger_count"]),
            fallback_burst_window_sec=float(planner["fallback_burst_window_sec"]),
            hard_failure_trigger_count=int(planner["hard_failure_trigger_count"]),
            upper_horizon_sec=float(planner["upper_horizon_sec"]),
            support_radius_km=float(planner["support_radius_km"]),
            min_orders_to_trigger=int(planner["min_orders_to_trigger"]),
        ),
        reward=RewardMeta(
            lambda_wait=float(reward["lambda_wait"]),
            wait_idle_penalty_coef=float(reward["wait_idle_penalty_coef"]),
            lambda_queue=float(reward["lambda_queue"]),
            lambda_miss=float(reward["lambda_miss"]),
            lambda_res_timeout=float(reward["lambda_res_timeout"]),
            lambda_overdue=float(reward["lambda_overdue"]),
            R_delivery_bonus=float(reward["R_delivery_bonus"]),
            max_overdue_sec=float(reward["max_overdue_sec"]),
            hard_overdue_penalty_sec=float(reward["hard_overdue_penalty_sec"]),
            hard_failure_penalty_sec=float(reward["hard_failure_penalty_sec"]),
            primary_metrics_scope=str(reward["primary_metrics_scope"]),
            include_mode_a_in_primary_metrics=bool(reward["include_mode_a_in_primary_metrics"]),
        ),
        critic_schema=critic_schema,
        shared_runtime_params_snapshot=SharedRuntimeParamsSnapshot(
            source_config="backend/config/drone_params.yaml",
            light_drone=DroneRuntimeParamsSnapshot(
                k1=float(params.light.k1),
                k2=float(params.light.k2),
                cruise_speed=float(params.light.cruise_speed),
                payload_capacity=float(params.light.payload_capacity),
                empty_weight=float(params.light.empty_weight),
                battery_capacity_j=float(params.light.battery_capacity_j),
                safe_margin_ratio=float(params.light.safe_margin_ratio),
            ),
            heavy_drone=DroneRuntimeParamsSnapshot(
                k1=float(params.heavy.k1),
                k2=float(params.heavy.k2),
                cruise_speed=float(params.heavy.cruise_speed),
                payload_capacity=float(params.heavy.payload_capacity),
                empty_weight=float(params.heavy.empty_weight),
                battery_capacity_j=float(params.heavy.battery_capacity_j),
                safe_margin_ratio=float(params.heavy.safe_margin_ratio),
            ),
            solver_energy=SolverEnergyRuntimeSnapshot(
                c_dist_et=float(params.solver_energy.c_dist_et),
                c_dist_uav=float(params.solver_energy.c_dist_uav),
                c_energy_et=float(params.solver_energy.c_energy_et),
                c_energy_uav=float(params.solver_energy.c_energy_uav),
                lambda_time=float(params.solver_energy.lambda_time),
                truck_energy_kwh_per_km=float(params.solver_energy.truck_energy_kwh_per_km),
                uav_energy_model=str(params.solver_energy.uav_energy_model),
                uav_alpha_wh_per_kg_km=float(params.solver_energy.uav_alpha_wh_per_kg_km),
                allow_moving_truck_launch=bool(params.solver_energy.allow_moving_truck_launch),
                truck_service_time_order_s=float(params.solver_energy.truck_service_time_order_s),
                drone_service_time_order_s=float(params.solver_energy.drone_service_time_order_s),
                truck_drone_launch_time_s=float(params.solver_energy.truck_drone_launch_time_s),
                truck_drone_recover_time_s=float(params.solver_energy.truck_drone_recover_time_s),
            ),
        ),
        env_semantic_contract=EnvSemanticContractMeta(
            mode_c_recovery_nodes=("station", "depot"),
            reservation_timeout_enabled=bool(reservation["enable"]),
            reservation_alpha=float(reservation["alpha"]),
            reservation_beta=float(reservation["beta"]),
            reservation_gamma=float(reservation["gamma"]),
            overdue_penalty_mode="per_dt",
            fifo_queue_enabled=True,
            riding_with_truck_enabled=True,
            allow_empty_backbone_route=bool(planner["allow_empty_backbone_route"]),
            hard_failure_type="airborne_energy_failure_or_no_safe_host",
        ),
        online_lock_params=OnlineLockParams(
            locked_fields=(
                "policy.d_model",
                "policy.nhead",
                "policy.lstm_hidden",
                "policy.max_global_orders",
                "policy.max_global_uavs",
                "policy.max_global_stations",
            ),
            tunable_fields=(
                "reward.lambda_wait",
                "reward.lambda_queue",
                "reward.lambda_miss",
                "reward.lambda_overdue",
            ),
        ),
    )
    run = TrainingRunMeta(
        model_version=model_version,
        trained_at=datetime.now(timezone.utc).isoformat(),
        scene_id=str(scene_ctx.scene_id),
        scene_bundle_dir=str(scene_ctx.scene_bundle_dir),
        training_input=order_source.training_input_meta,
    )
    return build_meta_json_dict(meta, run)


def _append_metrics(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")


def _write_json(*, path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_policy_checkpoint(
    *,
    path: Path,
    model: Any,
    optimizer: Any,
    critic_schema_hash: str,
    model_version: str,
    global_step: int,
    update_idx: int,
) -> None:
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "critic_schema_hash": critic_schema_hash,
            "model_version": model_version,
            "global_step": int(global_step),
            "update": int(update_idx),
        },
        path,
    )


def _set_training_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    mps_module = getattr(torch, "mps", None)
    mps_backend = getattr(torch.backends, "mps", None)
    if (
        mps_module is not None
        and hasattr(mps_module, "manual_seed")
        and mps_backend is not None
        and mps_backend.is_available()
    ):
        mps_module.manual_seed(seed)


def _validate_meta_payload_before_write(
    *,
    meta_payload: Mapping[str, Any],
    scene_ctx: Any,
    order_source: Any,
    critic_schema: CriticTensorSchemaMeta,
    train_cfg: _TrainingConfig,
    model_version: str,
    global_step: int,
    policy_path: Path,
    config_snapshot_path: Path,
    metrics_path: Path,
) -> None:
    if global_step < train_cfg.total_timesteps:
        raise RuntimeError(
            "训练尚未达到 total_timesteps，禁止写出 meta.json: "
            f"global_step={global_step}, total_timesteps={train_cfg.total_timesteps}"
        )
    if not policy_path.is_file():
        raise FileNotFoundError(f"缺少 policy.pt，禁止写出 meta.json: {policy_path}")
    if not config_snapshot_path.is_file():
        raise FileNotFoundError(
            f"缺少 training_config_snapshot.yaml，禁止写出 meta.json: {config_snapshot_path}"
        )
    if not metrics_path.is_file():
        raise FileNotFoundError(f"缺少 train_metrics.jsonl，禁止写出 meta.json: {metrics_path}")

    required_root_keys = (
        "model_version",
        "trained_at",
        "scene_id",
        "scene_bundle_dir",
        "training_input",
        "policy",
        "action_space",
        "candidate",
        "planner",
        "reward",
        "critic_schema",
        "shared_runtime_params_snapshot",
        "env_semantic_contract",
        "online_lock_params",
    )
    for key in required_root_keys:
        if key not in meta_payload:
            raise ValueError(f"meta.json 缺少一级字段: {key}")

    payload_model_version = str(meta_payload["model_version"])
    if payload_model_version != model_version:
        raise ValueError(
            "meta.json model_version 与本次 run 不一致: "
            f"payload={payload_model_version}, expected={model_version}"
        )
    _validate_iso8601_timestamp(str(meta_payload["trained_at"]))

    payload_scene_id = str(meta_payload["scene_id"])
    expected_scene_id = str(scene_ctx.scene_id)
    if payload_scene_id != expected_scene_id:
        raise ValueError(
            "meta.json scene_id 与 scene_ctx 不一致: "
            f"payload={payload_scene_id}, expected={expected_scene_id}"
        )

    payload_scene_bundle_dir = str(meta_payload["scene_bundle_dir"])
    expected_scene_bundle_dir = str(scene_ctx.scene_bundle_dir)
    if payload_scene_bundle_dir != expected_scene_bundle_dir:
        raise ValueError(
            "meta.json scene_bundle_dir 与 scene_ctx 不一致: "
            f"payload={payload_scene_bundle_dir}, expected={expected_scene_bundle_dir}"
        )

    training_input = _require_mapping(meta_payload, "training_input")
    payload_mode = str(training_input["order_source_mode"])
    expected_mode = str(order_source.mode.value)
    if payload_mode != expected_mode:
        raise ValueError(
            "meta.json training_input.order_source_mode 不一致: "
            f"payload={payload_mode}, expected={expected_mode}"
        )
    _require_exact_int(
        name="training_input.training_seed",
        actual=training_input["training_seed"],
        expected=train_cfg.training_seed,
    )
    _require_exact_int(
        name="training_input.total_timesteps",
        actual=training_input["total_timesteps"],
        expected=train_cfg.total_timesteps,
    )
    _require_exact_int(
        name="training_input.poisson_seed",
        actual=training_input["poisson_seed"],
        expected=order_source.seed,
    )

    expected_training_input = order_source.training_input_meta
    _require_exact_float(
        name="training_input.poisson_arrival_rate",
        actual=training_input["poisson_arrival_rate"],
        expected=expected_training_input.poisson_arrival_rate,
    )
    _require_exact_float(
        name="training_input.poisson_weight_max_kg",
        actual=training_input["poisson_weight_max_kg"],
        expected=expected_training_input.poisson_weight_max_kg,
    )
    _require_exact_int(
        name="training_input.order_window_min_min",
        actual=training_input["order_window_min_min"],
        expected=expected_training_input.order_window_min_min,
    )
    _require_exact_int(
        name="training_input.order_window_max_min",
        actual=training_input["order_window_max_min"],
        expected=expected_training_input.order_window_max_min,
    )

    benchmark = _require_mapping(training_input, "benchmark")
    expected_benchmark = order_source.benchmark
    if str(benchmark["orders_json"]) != expected_benchmark.orders_json:
        raise ValueError("meta.json training_input.benchmark.orders_json 不一致")
    if str(benchmark["orders_json_sha256"]) != expected_benchmark.orders_json_sha256:
        raise ValueError("meta.json training_input.benchmark.orders_json_sha256 不一致")
    _require_exact_int(
        name="training_input.benchmark.static_order_count",
        actual=benchmark["static_order_count"],
        expected=expected_benchmark.static_order_count,
    )
    _require_exact_int(
        name="training_input.benchmark.dynamic_order_count",
        actual=benchmark["dynamic_order_count"],
        expected=expected_benchmark.dynamic_order_count,
    )
    if bool(benchmark["benchmark_use_dynamic_orders"]) != bool(
        expected_benchmark.benchmark_use_dynamic_orders
    ):
        raise ValueError("meta.json training_input.benchmark.benchmark_use_dynamic_orders 不一致")

    critic_schema_meta = _require_mapping(meta_payload, "critic_schema")
    if str(critic_schema_meta["schema_hash"]) != str(critic_schema.schema_hash):
        raise ValueError(
            "meta.json critic_schema.schema_hash 与训练时 schema 不一致: "
            f"payload={critic_schema_meta['schema_hash']}, expected={critic_schema.schema_hash}"
        )
    if not str(critic_schema_meta["name"]):
        raise ValueError("meta.json critic_schema.name 不能为空")
    if not str(critic_schema_meta["schema_version"]):
        raise ValueError("meta.json critic_schema.schema_version 不能为空")

    shared_runtime = _require_mapping(meta_payload, "shared_runtime_params_snapshot")
    if not str(shared_runtime["source_config"]):
        raise ValueError("meta.json shared_runtime_params_snapshot.source_config 不能为空")
    _require_mapping(shared_runtime, "light_drone")
    _require_mapping(shared_runtime, "heavy_drone")
    _require_mapping(shared_runtime, "solver_energy")


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} 必须为 mapping")
    return value


def _require_exact_int(*, name: str, actual: Any, expected: int) -> None:
    actual_int = int(actual)
    expected_int = int(expected)
    if actual_int != expected_int:
        raise ValueError(f"{name} 不一致: payload={actual_int}, expected={expected_int}")


def _require_exact_float(*, name: str, actual: Any, expected: float) -> None:
    actual_float = float(actual)
    expected_float = float(expected)
    if actual_float != expected_float:
        raise ValueError(f"{name} 不一致: payload={actual_float}, expected={expected_float}")


def _validate_iso8601_timestamp(value: str) -> None:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"trained_at 不是合法 ISO 8601 时间戳: {value}") from exc


def _emit_runtime_device_debug(
    *,
    requested_device: str,
    resolved_device: Any,
) -> None:
    cuda_available = bool(torch.cuda.is_available())
    mps_backend = getattr(torch.backends, "mps", None)
    mps_built = bool(mps_backend is not None and mps_backend.is_built())
    mps_available = bool(mps_backend is not None and mps_backend.is_available())

    print(
        "[train_cmrappo] device debug | "
        f"requested={requested_device} | "
        f"resolved={resolved_device} | "
        f"torch={torch.__version__} | "
        f"cuda_available={cuda_available} | "
        f"mps_built={mps_built} | "
        f"mps_available={mps_available}"
    )


def _resolve_device(device_name: str) -> Any:
    if device_name == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        mps_backend = getattr(torch.backends, "mps", None)
        if mps_backend is not None and mps_backend.is_available():
            return torch.device("mps")
        return torch.device("cpu")
    if device_name.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"请求使用 {device_name}，但当前环境 CUDA 不可用")
    if device_name == "mps":
        mps_backend = getattr(torch.backends, "mps", None)
        if mps_backend is None or not mps_backend.is_available():
            raise RuntimeError("请求使用 mps，但当前环境 MPS 不可用")
    return torch.device(device_name)


def _require_torch() -> None:
    if torch is None:
        raise ImportError(
            "缺少 torch，无法运行 Phase 7 train_cmrappo.py。"
            "当前代码已实现训练入口，但本地环境尚未安装训练依赖。"
        )


def _load_training_config(config_path: Path) -> _TrainingConfig:
    raw = _load_yaml(config_path)
    training = raw["training"]
    batch_size_alias = training.get("batch_size")
    target_minibatch_timesteps = training.get("target_minibatch_timesteps")
    if target_minibatch_timesteps is None and batch_size_alias is None:
        raise ValueError("training.target_minibatch_timesteps 与 training.batch_size 不能同时缺失")
    if target_minibatch_timesteps is None:
        target_minibatch_timesteps = batch_size_alias
    if (
        batch_size_alias is not None
        and int(batch_size_alias) != int(target_minibatch_timesteps)
    ):
        raise ValueError(
            "recurrent PPO 配置错误：batch_size 与 target_minibatch_timesteps 不一致"
        )

    cfg = _TrainingConfig(
        device=str(training["device"]),
        total_timesteps=int(training["total_timesteps"]),
        rollout_steps=int(training["rollout_steps"]),
        recurrent_ppo=bool(training["recurrent_ppo"]),
        sequence_len=int(training["sequence_len"]),
        burn_in_len=int(training["burn_in_len"]),
        train_len=int(training["train_len"]),
        sequence_minibatch_size=int(training["sequence_minibatch_size"]),
        target_minibatch_timesteps=int(target_minibatch_timesteps),
        ppo_learning_rate=float(training["ppo_learning_rate"]),
        batch_size_alias=None if batch_size_alias is None else int(batch_size_alias),
        ppo_epochs=int(training["ppo_epochs"]),
        gamma=float(training["gamma"]),
        gae_lambda=float(training["gae_lambda"]),
        clip_coef=float(training["clip_coef"]),
        vf_clip_coef=float(training.get("vf_clip_coef", 0.2)),
        entropy_coef=float(training["entropy_coef"]),
        value_loss_coef=float(training["value_loss_coef"]),
        max_grad_norm=float(training["max_grad_norm"]),
        normalize_advantage=bool(training["normalize_advantage"]),
        target_kl=float(training["target_kl"]),
        training_seed=int(training["training_seed"]),
        log_interval_updates=int(training["log_interval_updates"]),
        save_interval_updates=int(training["save_interval_updates"]),
        eval_interval_updates=int(training["eval_interval_updates"]),
        benchmark_eval_episodes=int(training["benchmark_eval_episodes"]),
        stochastic_eval_seeds=int(training["stochastic_eval_seeds"]),
        allowed_train_order_source_mode=tuple(training["allowed_train_order_source_mode"]),
    )
    _validate_training_config(cfg)
    return cfg


def _validate_training_config(cfg: _TrainingConfig) -> None:
    if not cfg.recurrent_ppo:
        raise RuntimeError(
            "当前 train_cmrappo.py 仅接受 recurrent_ppo=true 的 Phase 7 V1 正式口径"
        )
    if cfg.burn_in_len != 0:
        raise ValueError("Phase 7 V1 固定要求 burn_in_len=0")
    if cfg.sequence_len <= 0:
        raise ValueError("sequence_len 必须为正数")
    if cfg.train_len != cfg.sequence_len - cfg.burn_in_len:
        raise ValueError("train_len 必须满足 sequence_len - burn_in_len")
    if cfg.sequence_minibatch_size <= 0:
        raise ValueError("sequence_minibatch_size 必须为正数")
    if cfg.target_minibatch_timesteps <= 0:
        raise ValueError("target_minibatch_timesteps 必须为正数")
    if cfg.eval_interval_updates < 0:
        raise ValueError("eval_interval_updates 不能为负数")
    if cfg.benchmark_eval_episodes <= 0:
        raise ValueError("benchmark_eval_episodes 必须为正数")
    if cfg.stochastic_eval_seeds <= 0:
        raise ValueError("stochastic_eval_seeds 必须为正数")
    expected_target = cfg.sequence_minibatch_size * cfg.train_len
    if expected_target != cfg.target_minibatch_timesteps:
        raise ValueError(
            "Phase 7 V1 推荐满载关系被破坏："
            f"sequence_minibatch_size * train_len = {expected_target}, "
            f"target_minibatch_timesteps = {cfg.target_minibatch_timesteps}"
        )


def _load_eval_config(config_path: Path) -> _EvalConfig:
    raw = _load_yaml(config_path)
    data = raw["data"]
    stochastic_eval = data["stochastic_eval_arrival_rate_per_min"]
    return _EvalConfig(
        benchmark_seed=int(data["benchmark_eval_seed"]),
        stochastic_seed_base=int(data["stochastic_eval_seed_base"]),
        stochastic_arrival_rates=(
            ("low", float(stochastic_eval["low"])),
            ("medium", float(stochastic_eval["medium"])),
            ("high", float(stochastic_eval["high"])),
        ),
    )


def _load_policy_config(config_path: Path) -> _PolicyConfig:
    raw = _load_yaml(config_path)
    policy = raw["policy"]
    return _PolicyConfig(
        d_model=int(policy["d_model"]),
        ff_dim=int(policy["ff_dim"]),
        lstm_hidden=int(policy["lstm_hidden"]),
        lstm_layers=int(policy["lstm_layers"]),
        hist_len=int(policy["hist_len"]),
    )


def _load_yaml(config_path: Path) -> Mapping[str, Any]:
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("缺少 PyYAML，无法读取训练配置") from exc

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, Mapping):
        raise ValueError(f"YAML 顶层必须为 mapping: {config_path}")
    return raw


__all__ = ["train_cmrappo"]
