#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — TrainingEnvAdapter 在线前端运行时适配器。

职责：
  - 在不修改训练脚本的前提下，为已训练的 CMRAPPO 策略提供在线运行时；
  - 适配 `SimulationEngine` 现有的控制 / 快照接口；
  - 将 `TrainingEnvAdapter` 真值状态桥接为当前前端可消费的 `FULL_SNAPSHOT / TICK` 结构。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from api.websockets.telemetry import broadcast_tick
from core.entities.order import Order
from core.entities.primitives import Position3D, SourceType
from environment.state.entity_manager import EntityManager
from training.critic_batch_builder import CriticBatchBuilder
from training.contracts import ResolvedActionIndices
from training.env_adapter import TrainingEnvAdapter
from training.model import SharedPPOActorCritic
from training.observation_tensorizer import ObservationTensorizer
from training.order_source_adapter import OrderSourceMode, build_order_source
from training.policy_inference import LoadedPolicyRuntime
from training.scene_loader import (
    DEFAULT_CONFIG_PATH,
    BenchmarkDynamicOrder,
    TrainingRoadNetwork,
    TrainingSceneContext,
    load_default_scene,
    load_training_scene,
)
from training.train_cmrappo import (
    _build_candidate_output,
    _detach_lstm_state,
    _load_policy_config,
    _require_torch,
    _reset_recurrent_state_for_failed_drones,
    _resolve_device,
)
from utils.coord_utils import wgs84_to_utm


try:  # pragma: no cover
    import torch
except ImportError:  # pragma: no cover
    torch = None


logger = logging.getLogger(__name__)

_TICK_INTERVAL_SEC = 0.1
_MIN_SPEED_RATIO = 1e-3
_MAX_ORDER_LIMIT = 500

_TRAINING_TO_FRONTEND_DRONE_STATUS: Mapping[str, str] = {
    "idle": "IDLE",
    "active_wait": "IDLE",
    "riding_with_truck": "IDLE",
    "flying_to_deliver": "FLYING",
    "return_to_rendezvous": "FLYING",
    "return_to_station": "FLYING",
    "return_to_depot": "FLYING",
    "fallback_recovery": "FLYING",
    "airborne_energy_failure": "FLYING",
    "delivery_service": "LANDING",
    "delivered": "LANDING",
    "waiting_for_truck": "LANDING",
    "queueing_at_host": "CHARGING",
    "charging_or_swap": "CHARGING",
    "charging_on_truck": "CHARGING",
}


@dataclass(frozen=True)
class PolicyActivationConfig:
    policy_name: str
    policy_path: Path
    config_path: Path
    scene_id: str
    scene_bundle_dir: str | None
    order_source_mode: OrderSourceMode
    deterministic: bool
    speed_ratio: float
    seed: int | None
    arrival_rate_per_min: float | None
    device: str


def resolve_scene_context(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    scene_id: str | None = None,
    scene_bundle_dir: str | Path | None = None,
) -> TrainingSceneContext:
    """
    解析在线运行要使用的训练场景。

    规则：
      - 显式提供 `scene_bundle_dir` 时，以该 bundle 为准；
      - 否则回退到 `config_path` 中声明的默认训练场景；
      - 若同时给定 `scene_id`，会对结果做一致性校验。
    """

    cfg_path = Path(config_path)
    if scene_bundle_dir is not None:
        resolved = load_training_scene(
            scene_bundle_dir=scene_bundle_dir,
            expected_scene_id=scene_id,
        )
        return resolved

    default_scene = load_default_scene(config_path=cfg_path)
    if scene_id is not None and str(scene_id).strip() and default_scene.scene_id != str(scene_id).strip():
        raise ValueError(
            "未提供 scene_bundle_dir，且请求 scene_id 与配置默认场景不一致: "
            f"requested={scene_id}, config_default={default_scene.scene_id}"
        )
    return default_scene


def load_policy_runtime_for_scene(
    *,
    scene_ctx: TrainingSceneContext,
    policy_path: str | Path,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    device: str = "auto",
) -> LoadedPolicyRuntime:
    """
    为指定 `scene_ctx` 加载推理 runtime。

    不能直接复用 `policy_inference.load_trained_policy()`，
    因为在线前端场景需要与当前激活的 scene bundle 保持一致。
    """

    _require_torch()
    cfg_path = Path(config_path)
    policy_cfg = _load_policy_config(cfg_path)
    tensorizer = ObservationTensorizer(scene_ctx=scene_ctx, config_path=cfg_path)
    critic_builder = CriticBatchBuilder(scene_ctx=scene_ctx, config_path=cfg_path)
    critic_schema = critic_builder.default_schema_meta

    bootstrap_source = build_order_source(
        scene_ctx,
        mode=OrderSourceMode.POISSON,
        config_path=cfg_path,
    )
    bootstrap_env = TrainingEnvAdapter(
        scene_ctx=scene_ctx,
        order_source=bootstrap_source,
        config_path=cfg_path,
    )
    bootstrap_result = bootstrap_env.reset()
    if bootstrap_result.decision_context is None:
        raise RuntimeError("bootstrap env 没有可用的 decision_context")
    bootstrap_candidate = _build_candidate_output(
        env=bootstrap_env,
        decision_context=bootstrap_result.decision_context,
        last_seen_plan_version_by_drone={},
    )
    bootstrap_observation = tensorizer.build(
        decision_context=bootstrap_result.decision_context,
        candidate_out=bootstrap_candidate,
        transition_history=(),
    )
    bootstrap_critic = critic_builder.build(
        decision_context=bootstrap_result.decision_context,
        critic_tensor_schema_meta=critic_schema,
    )

    model = SharedPPOActorCritic(
        uav_feat_dim=int(bootstrap_observation.uav_self_token.shape[-1]),
        order_feat_dim=int(bootstrap_observation.order_tokens.shape[-1]),
        recovery_feat_dim=int(bootstrap_observation.recovery_tokens.shape[-1]),
        infra_feat_dim=int(bootstrap_observation.infra_tokens.shape[-1]),
        history_feat_dim=int(bootstrap_observation.history_tokens.shape[-1]),
        critic_order_feat_dim=int(bootstrap_critic.global_order_pool_tokens.shape[-1]),
        critic_uav_feat_dim=int(bootstrap_critic.global_uav_tokens.shape[-1]),
        critic_station_feat_dim=int(bootstrap_critic.global_station_tokens.shape[-1]),
        critic_plan_feat_dim=int(bootstrap_critic.coarse_plan_summary_vec.shape[-1]),
        critic_sys_feat_dim=int(bootstrap_critic.global_system_summary_vec.shape[-1]),
        d_model=policy_cfg.d_model,
        ff_dim=policy_cfg.ff_dim,
        lstm_hidden=policy_cfg.lstm_hidden,
        lstm_layers=policy_cfg.lstm_layers,
    )

    device_obj = _resolve_device(device)
    model.to(device_obj)

    resolved_policy_path = Path(policy_path).resolve()
    checkpoint = torch.load(resolved_policy_path, map_location=device_obj)
    state_dict = checkpoint.get("model_state_dict")
    if not isinstance(state_dict, dict):
        raise ValueError(f"policy checkpoint 缺少 model_state_dict: {resolved_policy_path}")
    model.load_state_dict(state_dict)
    model.eval()

    return LoadedPolicyRuntime(
        config_path=cfg_path,
        policy_path=resolved_policy_path,
        scene_ctx=scene_ctx,
        tensorizer=tensorizer,
        critic_builder=critic_builder,
        critic_schema=critic_schema,
        model=model,
        policy_cfg=policy_cfg,
        device=device_obj,
        checkpoint_meta={
            "model_version": checkpoint.get("model_version"),
            "global_step": checkpoint.get("global_step"),
            "update": checkpoint.get("update"),
            "critic_schema_hash": checkpoint.get("critic_schema_hash"),
        },
    )


class TrainingTelemetryBridge:
    """将 `TrainingEnvAdapter` 真值桥接为当前前端遥测 payload。"""

    def __init__(self, *, policy_name: str, checkpoint_path: str) -> None:
        self._policy_name = str(policy_name)
        self._checkpoint_path = str(checkpoint_path)

    def build_tick_payload(
        self,
        *,
        env: TrainingEnvAdapter,
        speed_ratio: float,
        deterministic: bool,
        order_source_mode: str,
    ) -> dict[str, Any]:
        runtime_state = env.build_runtime_state_view()
        decision = env.current_decision_context
        visual_snapshot = env.build_visualization_snapshot()
        entity_mgr = env._require_entity_manager()
        order_mgr = env._require_order_manager()
        entities = entity_mgr.get_telemetry()
        self._overlay_runtime_entities(entities, runtime_state)
        stats = order_mgr.get_status_summary()
        stats.update(
            {
                "active_policy": self._policy_name,
                "checkpoint": self._checkpoint_path,
                "order_source_mode": str(order_source_mode),
                "deterministic": bool(deterministic),
                "current_decision": (
                    {
                        "drone_id": str(decision.deciding_drone_id),
                        "trigger_type": str(decision.trigger_type),
                        "trigger_station_id": decision.trigger_station_id,
                    }
                    if decision is not None
                    else None
                ),
                "last_reward_breakdown": dict(visual_snapshot.get("last_reward_breakdown") or {}),
            }
        )
        stats.update(self._select_episode_stats(env.build_episode_metrics_snapshot()))
        return {
            "sim_time": round(float(runtime_state.t_now), 3),
            "entities": entities,
            "orders": order_mgr.get_recent_orders(limit=_MAX_ORDER_LIMIT),
            "stats": stats,
        }

    def build_full_snapshot(
        self,
        *,
        env: TrainingEnvAdapter,
        is_running: bool,
        speed_ratio: float,
        sim_start_wall_ms: int,
        deterministic: bool,
        order_source_mode: str,
    ) -> dict[str, Any]:
        runtime_state = env.build_runtime_state_view()
        entity_mgr = env._require_entity_manager()
        entities = entity_mgr.get_static_snapshot()
        self._overlay_runtime_entities(entities, runtime_state)
        payload = self.build_tick_payload(
            env=env,
            speed_ratio=speed_ratio,
            deterministic=deterministic,
            order_source_mode=order_source_mode,
        )
        payload.update(
            {
                "is_running": bool(is_running),
                "speed_ratio": float(speed_ratio),
                "sim_start_wall_ms": int(sim_start_wall_ms),
                "entities": entities,
            }
        )
        return {"type": "FULL_SNAPSHOT", "payload": payload}

    def _overlay_runtime_entities(self, entities: dict[str, Any], runtime_state: Any) -> None:
        truck_payload = entities.get("trucks")
        if isinstance(truck_payload, list) and truck_payload:
            truck = truck_payload[0]
            truck_lng, truck_lat = runtime_state.truck_current_loc.to_wgs84()
            truck["lng"] = float(truck_lng)
            truck["lat"] = float(truck_lat)
            truck["altitude"] = float(runtime_state.truck_current_loc.z)

        drone_by_id: dict[str, Any] = {}
        for drone in entities.get("drones", []) or []:
            drone_id = drone.get("drone_id")
            if isinstance(drone_id, str):
                drone_by_id[drone_id] = drone

        for drone_id, drone_state in runtime_state.drone_states.items():
            drone_payload = drone_by_id.get(drone_id)
            if drone_payload is None:
                continue
            lng, lat = drone_state.current_loc.to_wgs84()
            drone_payload["lng"] = float(lng)
            drone_payload["lat"] = float(lat)
            drone_payload["altitude"] = float(drone_state.current_loc.z)
            drone_payload["battery_ratio"] = round(float(drone_state.battery_ratio), 4)
            drone_payload["carrying_order_id"] = drone_state.carrying_order_id
            drone_payload["status"] = _TRAINING_TO_FRONTEND_DRONE_STATUS.get(
                str(drone_state.training_state),
                str(drone_payload.get("status") or "IDLE"),
            )
            drone_payload["training_status"] = str(drone_state.training_state)
            drone_payload["battery_current"] = float(drone_state.battery_current)
            drone_payload["battery_max"] = float(drone_state.battery_max)
            drone_payload["cruise_speed"] = float(drone_state.cruise_speed)
            drone_payload["payload_capacity"] = float(drone_state.payload_capacity)
            drone_payload["reservation_node_id"] = (
                None
                if drone_state.reservation is None
                else str(drone_state.reservation.recover_node)
            )

    def _select_episode_stats(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "delivery_count",
            "fallback_count",
            "hard_failure_count",
            "reservation_timeout_count",
            "wait_action_count",
            "dispatch_decision_count",
            "dispatch_mode_b_count",
            "dispatch_mode_c_count",
            "mode_c_success_count",
            "done_reason",
            "episode_end_t_sec",
        )
        return {
            key: snapshot[key]
            for key in keys
            if key in snapshot
        }


class TrainingPolicyRuntimeAdapter:
    """
    将 `TrainingEnvAdapter` 伪装成 `SimulationEngine` 风格接口的在线运行时。

    暴露：
      - start / pause / reset / set_speed
      - current_time / is_running / sim_start_wall_ms
      - build_full_snapshot / get_recent_orders
    """

    def __init__(
        self,
        *,
        activation: PolicyActivationConfig,
        scene_ctx: TrainingSceneContext,
    ) -> None:
        self._activation = activation
        self._scene_ctx = scene_ctx
        self._runtime = load_policy_runtime_for_scene(
            scene_ctx=scene_ctx,
            policy_path=activation.policy_path,
            config_path=activation.config_path,
            device=activation.device,
        )
        self._telemetry = TrainingTelemetryBridge(
            policy_name=activation.policy_name,
            checkpoint_path=str(self._runtime.policy_path),
        )

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.is_running = False
        self.speed_ratio = float(activation.speed_ratio)
        self.sim_start_wall_ms = 0

        self._env: TrainingEnvAdapter | None = None
        self._result: Any = None
        self._history_buffer: deque[Any] | None = None
        self._last_seen_plan_version_by_drone: dict[str, int] = {}
        self._lstm_state_by_drone: dict[str, Any] = {}
        self._recurrent_segment_id_by_drone: dict[str, int] = {}
        self._reset_runtime_state_locked()

    @property
    def current_time(self) -> float:
        with self._lock:
            env = self._require_env_locked()
            return float(env.build_runtime_state_view().t_now)

    @property
    def policy_name(self) -> str:
        return self._activation.policy_name

    @property
    def checkpoint_path(self) -> str:
        return str(self._activation.policy_path)

    def start(self) -> None:
        with self._lock:
            if self._result is not None and getattr(self._result, "done", False):
                self._reset_runtime_state_locked()
            if self.is_running:
                return
            if self._thread is None or not self._thread.is_alive():
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="TrainingPolicyRuntimeAdapter",
                    daemon=True,
                )
                self._thread.start()
            self.is_running = True
            if self.sim_start_wall_ms <= 0:
                self.sim_start_wall_ms = int(time.time() * 1000)

    def pause(self) -> None:
        with self._lock:
            self.is_running = False

    def reset(self) -> None:
        with self._lock:
            self.is_running = False
            self.sim_start_wall_ms = 0
            self._reset_runtime_state_locked()

    def set_speed(self, speed_ratio: float) -> None:
        numeric = float(speed_ratio)
        if numeric <= 0:
            raise ValueError("speed_ratio 必须大于 0")
        with self._lock:
            self.speed_ratio = numeric

    def shutdown(self) -> None:
        with self._lock:
            self.is_running = False
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def get_recent_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            env = self._require_env_locked()
            order_mgr = env._require_order_manager()
            return order_mgr.get_recent_orders(limit)

    def build_tick_payload(self) -> dict[str, Any]:
        with self._lock:
            env = self._require_env_locked()
            return self._telemetry.build_tick_payload(
                env=env,
                speed_ratio=self.speed_ratio,
                deterministic=self._activation.deterministic,
                order_source_mode=self._activation.order_source_mode.value,
            )

    def build_full_snapshot(self) -> dict[str, Any]:
        with self._lock:
            env = self._require_env_locked()
            return self._telemetry.build_full_snapshot(
                env=env,
                is_running=self.is_running,
                speed_ratio=self.speed_ratio,
                sim_start_wall_ms=self.sim_start_wall_ms,
                deterministic=self._activation.deterministic,
                order_source_mode=self._activation.order_source_mode.value,
            )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                if not self.is_running:
                    sleep_interval = 0.05
                    payload = None
                else:
                    loop_started = time.monotonic()
                    payload = self._run_one_cycle_locked()
                    elapsed = time.monotonic() - loop_started
                    sleep_interval = max(
                        0.0,
                        (_TICK_INTERVAL_SEC / max(self.speed_ratio, _MIN_SPEED_RATIO)) - elapsed,
                    )

            if payload is not None:
                try:
                    broadcast_tick(payload)
                except Exception:
                    logger.exception("[TrainingPolicyRuntimeAdapter] 广播 TICK 失败")

            self._stop_event.wait(sleep_interval)

    def _run_one_cycle_locked(self) -> dict[str, Any]:
        env = self._require_env_locked()
        while self.is_running and self._result is not None and not self._result.done:
            decision_context = getattr(self._result, "decision_context", None)
            if decision_context is None:
                break
            self._step_policy_once_locked(env=env, decision_context=decision_context)

        if self._result is not None and getattr(self._result, "done", False):
            self.is_running = False

        return self._telemetry.build_tick_payload(
            env=env,
            speed_ratio=self.speed_ratio,
            deterministic=self._activation.deterministic,
            order_source_mode=self._activation.order_source_mode.value,
        )

    def _step_policy_once_locked(self, *, env: TrainingEnvAdapter, decision_context: Any) -> None:
        drone_id = str(decision_context.deciding_drone_id)
        candidate_out = _build_candidate_output(
            env=env,
            decision_context=decision_context,
            last_seen_plan_version_by_drone=self._last_seen_plan_version_by_drone,
        )
        transition_history = tuple(self._history_buffer or ())
        observation_batch = self._runtime.tensorizer.build(
            decision_context=decision_context,
            candidate_out=candidate_out,
            transition_history=transition_history,
        )
        action_mask = self._runtime.tensorizer.build_action_mask(candidate_out)
        critic_batch = self._runtime.critic_builder.build(
            decision_context=decision_context,
            critic_tensor_schema_meta=self._runtime.critic_schema,
        )
        with torch.no_grad():
            policy_out, next_lstm_state = self._runtime.model.forward(
                observation_batch=observation_batch,
                action_mask=action_mask,
                critic_batch=critic_batch,
                lstm_state=self._lstm_state_by_drone.get(drone_id),
            )
            sampled_action, _ = self._runtime.model.sample_action(
                policy_out=policy_out,
                action_mask=action_mask,
                deterministic=self._activation.deterministic,
            )
        action_indices = ResolvedActionIndices(**sampled_action)
        env_action = candidate_out.resolved_action_lookup.resolve(
            root_branch_idx=int(action_indices.root_branch_idx),
            order_idx=(
                None if action_indices.order_idx is None else int(action_indices.order_idx)
            ),
            mode_idx=(
                None if action_indices.mode_idx is None else int(action_indices.mode_idx)
            ),
            recovery_idx=(
                None if action_indices.recovery_idx is None else int(action_indices.recovery_idx)
            ),
        )
        step_result = env.step(env_action)
        if self._history_buffer is not None:
            self._history_buffer.append(
                self._runtime.tensorizer.build_transition_summary(
                    decision_context=decision_context,
                    candidate_out=candidate_out,
                    action_indices=action_indices,
                    step_result=step_result,
                )
            )
        self._last_seen_plan_version_by_drone[drone_id] = decision_context.coarse_plan.plan_version
        self._lstm_state_by_drone[drone_id] = _detach_lstm_state(lstm_state=next_lstm_state)
        _reset_recurrent_state_for_failed_drones(
            runtime_state=step_result.runtime_state,
            lstm_state_by_drone=self._lstm_state_by_drone,
            recurrent_segment_id_by_drone=self._recurrent_segment_id_by_drone,
        )
        self._result = step_result

    def _reset_runtime_state_locked(self) -> None:
        overrides: dict[str, Any] = {}
        if self._activation.arrival_rate_per_min is not None:
            overrides["poisson_arrival_rate"] = float(self._activation.arrival_rate_per_min)
        order_source = build_order_source(
            self._scene_ctx,
            mode=self._activation.order_source_mode,
            seed=self._activation.seed,
            overrides=overrides,
            config_path=self._activation.config_path,
        )
        self._env = TrainingEnvAdapter(
            scene_ctx=self._scene_ctx,
            order_source=order_source,
            config_path=self._activation.config_path,
        )
        self._result = self._env.reset()
        self._history_buffer = deque(maxlen=self._runtime.policy_cfg.hist_len)
        self._last_seen_plan_version_by_drone = {}
        self._lstm_state_by_drone = {}
        self._recurrent_segment_id_by_drone = {}

    def _require_env_locked(self) -> TrainingEnvAdapter:
        if self._env is None:
            raise RuntimeError("runtime env 尚未初始化")
        return self._env


def build_policy_activation_config(payload: Mapping[str, Any]) -> PolicyActivationConfig:
    policy_path_raw = payload.get("policy_path")
    if not isinstance(policy_path_raw, str) or not policy_path_raw.strip():
        raise ValueError("policy_path 为必填字段")

    config_path_raw = payload.get("config_path", str(DEFAULT_CONFIG_PATH))
    if not isinstance(config_path_raw, str) or not config_path_raw.strip():
        raise ValueError("config_path 不能为空")

    mode_raw = str(payload.get("order_source_mode", OrderSourceMode.BENCHMARK.value)).strip().lower()
    try:
        order_source_mode = OrderSourceMode(mode_raw)
    except ValueError as exc:
        raise ValueError(
            f"未知 order_source_mode: {mode_raw}，支持 {', '.join(mode.value for mode in OrderSourceMode)}"
        ) from exc

    speed_ratio = float(payload.get("speed_ratio", 1.0))
    if speed_ratio <= 0:
        raise ValueError("speed_ratio 必须大于 0")

    seed_raw = payload.get("seed")
    seed = None if seed_raw is None else int(seed_raw)
    arrival_rate_raw = payload.get("arrival_rate_per_min")
    arrival_rate_per_min = None if arrival_rate_raw is None else float(arrival_rate_raw)

    return PolicyActivationConfig(
        policy_name=str(payload.get("policy_name", "rh_alns_cmrappo")).strip() or "rh_alns_cmrappo",
        policy_path=Path(policy_path_raw).resolve(),
        config_path=Path(config_path_raw).resolve(),
        scene_id=str(payload.get("scene_id", "")).strip(),
        scene_bundle_dir=(
            None
            if payload.get("scene_bundle_dir") in (None, "")
            else str(payload.get("scene_bundle_dir"))
        ),
        order_source_mode=order_source_mode,
        deterministic=bool(payload.get("deterministic", True)),
        speed_ratio=speed_ratio,
        seed=seed,
        arrival_rate_per_min=arrival_rate_per_min,
        device=str(payload.get("device", "auto")).strip() or "auto",
    )


def build_runtime_scene_context_from_payload(
    *,
    scene_ctx: TrainingSceneContext,
    entities_raw: Mapping[str, Any],
    orders_raw: Mapping[str, Any],
    bounds_override: Mapping[str, Any] | None = None,
) -> TrainingSceneContext:
    """
    基于已解析的 bundle 场景骨架，覆写当前在线运行的实体 / 订单配置。

    说明：
      - 不修改训练脚本，也不污染磁盘场景资产；
      - 仅在在线前端激活路径上构造一次内存态 `TrainingSceneContext`。
    """

    entity_manager = EntityManager()
    entity_manager.load_from_config({"entities": entities_raw})
    normalized_static_entries = _normalize_initial_orders_batch(orders_raw.get("static_orders", []))
    static_orders = tuple(_build_static_order(entry) for entry in normalized_static_entries)
    dynamic_orders = tuple(
        _build_dynamic_order(entry)
        for entry in orders_raw.get("dynamic_orders", [])
    )

    scene_config = copy.deepcopy(scene_ctx.scene_config)
    if bounds_override is not None:
        scene_config["bounds"] = dict(bounds_override)

    return TrainingSceneContext(
        scene_id=str(scene_ctx.scene_id),
        scene_bundle_dir=str(scene_ctx.scene_bundle_dir),
        scene_config_path=str(scene_ctx.scene_config_path),
        entities_json_path=str(scene_ctx.entities_json_path),
        orders_json_path=str(scene_ctx.orders_json_path),
        scene_config=scene_config,
        bounds=copy.deepcopy(scene_config.get("bounds", scene_ctx.bounds)),
        depots=dict(entity_manager.depots),
        stations=dict(entity_manager.stations),
        trucks=dict(entity_manager.trucks),
        drones=dict(entity_manager.drones),
        static_orders=static_orders,
        dynamic_orders=dynamic_orders,
        entities_raw=copy.deepcopy(dict(entities_raw)),
        orders_raw=copy.deepcopy(dict(orders_raw)),
        road_network=TrainingRoadNetwork(
            geojson=copy.deepcopy(scene_ctx.road_network.geojson),
            geojson_path=str(scene_ctx.road_network.geojson_path),
            xml_path=scene_ctx.road_network.xml_path,
            fmt=str(scene_ctx.road_network.fmt),
        ),
        entity_manager=entity_manager,
    )


def _build_static_order(entry: Mapping[str, Any]) -> Order:
    create_time = float(entry.get("create_time", 0.0))
    deadline = float(entry.get("deadline", create_time))
    x, y = wgs84_to_utm(float(entry["delivery_lng"]), float(entry["delivery_lat"]))
    return Order(
        order_id=str(entry["order_id"]),
        create_time=create_time,
        deadline=deadline,
        delivery_loc=Position3D(
            x=float(x),
            y=float(y),
            z=float(entry.get("delivery_z", 0.0)),
        ),
        payload_weight=float(entry.get("payload_weight", 1.0)),
        pickup_source_id=entry.get("pickup_source_id"),
        source_type=_coerce_source_type(entry.get("source_type")),
    )


def _build_dynamic_order(entry: Mapping[str, Any]) -> BenchmarkDynamicOrder:
    create_time = float(entry.get("spawn_sim_s", 0.0))
    deadline_sim_s = entry.get("deadline_sim_s")
    deadline_offset_s = entry.get("deadline_offset_s")
    if deadline_sim_s is None and deadline_offset_s is None:
        deadline_offset_s = float(entry.get("deadline", 0.0)) - create_time

    x, y = wgs84_to_utm(float(entry["delivery_lng"]), float(entry["delivery_lat"]))
    order = Order(
        order_id=str(entry["order_id"]),
        create_time=create_time,
        deadline=(
            float(deadline_sim_s)
            if deadline_sim_s is not None
            else create_time + float(deadline_offset_s or 0.0)
        ),
        delivery_loc=Position3D(
            x=float(x),
            y=float(y),
            z=float(entry.get("delivery_z", 0.0)),
        ),
        payload_weight=float(entry.get("payload_weight", 1.0)),
        pickup_source_id=entry.get("pickup_source_id"),
        source_type=_coerce_source_type(entry.get("source_type")),
    )
    return BenchmarkDynamicOrder(
        order=order,
        spawn_sim_s=create_time,
        deadline_offset_s=None if deadline_offset_s is None else float(deadline_offset_s),
        deadline_sim_s=None if deadline_sim_s is None else float(deadline_sim_s),
        raw=copy.deepcopy(dict(entry)),
    )


def build_orders_raw_from_sim_init_payload(
    *,
    initial_orders: list[Mapping[str, Any]],
    scheduled_dynamic_orders: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """
    将现有 `/api/sim/init` 请求体中的订单字段桥接为训练侧 `orders_raw` 结构。

    约定：
      - `initial_orders` 语义是“当前应进入运行时订单池的订单”，
        在线 PPO 适配时将其桥接为 `spawn_sim_s=create_time` 的 benchmark dynamic replay；
      - `scheduled_dynamic_orders` 原样保留为后续注入流。
    """

    dynamic_orders: list[dict[str, Any]] = []
    for entry in _normalize_initial_orders_batch(initial_orders):
        dynamic_orders.append(
            {
                "order_id": str(entry["order_id"]),
                "spawn_sim_s": float(entry.get("create_time", 0.0)),
                "deadline_sim_s": float(entry.get("deadline", entry.get("create_time", 0.0))),
                "delivery_lng": float(entry["delivery_lng"]),
                "delivery_lat": float(entry["delivery_lat"]),
                "delivery_z": float(entry.get("delivery_z", 0.0)),
                "payload_weight": float(entry.get("payload_weight", 1.0)),
                "source_type": entry.get("source_type"),
                "pickup_source_id": entry.get("pickup_source_id"),
                "priority": entry.get("priority"),
                "priority_label": entry.get("priority_label"),
                "fulfillment_mode": entry.get("fulfillment_mode"),
            }
        )

    for entry in scheduled_dynamic_orders:
        dynamic_orders.append(copy.deepcopy(dict(entry)))

    dynamic_orders.sort(key=lambda item: (float(item.get("spawn_sim_s", 0.0)), str(item.get("order_id", ""))))
    return {
        "static_orders": [],
        "dynamic_orders": dynamic_orders,
    }


def _normalize_initial_orders_batch(entries: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not entries:
        return []

    normalized = [copy.deepcopy(dict(entry)) for entry in entries]
    first = normalized[0]
    first_create = float(first.get("create_time", 0.0))
    first_domain = str(first.get("time_domain", "") or "").strip().lower()
    if first_domain == "sim_s" or first_create < 1e11:
        return normalized

    base_ms = min(float(item.get("create_time", 0.0)) for item in normalized)
    for item in normalized:
        item["create_time"] = (float(item.get("create_time", 0.0)) - base_ms) / 1000.0
        item["deadline"] = (float(item.get("deadline", item["create_time"])) - base_ms) / 1000.0
        item["time_domain"] = "sim_s"
    return normalized


def _coerce_source_type(value: Any) -> SourceType | None:
    if value in (None, ""):
        return None
    try:
        return SourceType(str(value).strip().upper())
    except ValueError:
        logger.warning("[frontend_runtime_adapter] 未识别的 source_type=%r，按 None 处理", value)
        return None
