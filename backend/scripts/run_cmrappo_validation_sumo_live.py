#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用已训练权重进行在线验证，并实时叠加订单/无人机到 SUMO GUI。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from training.policy_inference import load_trained_policy, run_policy_episode
from training.scene_loader import DEFAULT_CONFIG_PATH
from training.sumo_live_renderer import RealtimeSumoEpisodeRenderer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="已训练策略的实时 SUMO 在线验证")
    parser.add_argument(
        "--policy-path",
        type=str,
        default="",
        help="policy.pt 路径；为空时自动选择最新 run 的 policy.pt",
    )
    parser.add_argument(
        "--policy-dir",
        type=str,
        default="",
        help="包含 policy.pt 的 run 目录；与 --policy-path 二选一",
    )
    parser.add_argument(
        "--config-path",
        type=str,
        default=str(DEFAULT_CONFIG_PATH),
        help="训练配置文件路径",
    )
    parser.add_argument(
        "--sumo-gui-bin",
        type=str,
        default="",
        help="sumo-gui 可执行文件路径；为空时自动从 PATH 查找",
    )
    parser.add_argument(
        "--order-source-mode",
        type=str,
        default="benchmark",
        choices=("benchmark", "poisson", "hybrid"),
        help="在线验证使用的订单源模式",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="订单源种子；为空时按配置默认值",
    )
    parser.add_argument(
        "--arrival-rate-per-min",
        type=float,
        default=None,
        help="仅 poisson / hybrid 模式生效，覆盖到达率（单/分钟）",
    )
    parser.add_argument(
        "--stochastic",
        action="store_true",
        help="关闭贪心验证，改为按策略分布采样动作",
    )
    parser.add_argument(
        "--gui-step-sec",
        type=float,
        default=5.0,
        help="GUI 每次推进的仿真秒数",
    )
    parser.add_argument(
        "--playback-speed",
        type=float,
        default=20.0,
        help="播放倍速",
    )
    parser.add_argument(
        "--hold-final-sec",
        type=float,
        default=6.0,
        help="验证结束后停留秒数",
    )
    args = parser.parse_args(argv)

    policy_path = _resolve_policy_path(
        policy_path=args.policy_path,
        policy_dir=args.policy_dir,
    )
    runtime = load_trained_policy(
        policy_path=policy_path,
        config_path=args.config_path,
    )
    renderer = RealtimeSumoEpisodeRenderer(
        config_path=args.config_path,
        sumo_gui_bin=args.sumo_gui_bin or None,
        gui_step_sec=args.gui_step_sec,
        playback_speed=args.playback_speed,
        follow_truck=False,
    )

    def _on_reset(env, _result) -> None:
        renderer.reset_episode()
        renderer.sync_snapshot(env.build_visualization_snapshot())

    def _on_step(env, _decision_context, _env_action, _step_result) -> None:
        renderer.sync_snapshot(env.build_visualization_snapshot())

    try:
        rollout = run_policy_episode(
            runtime=runtime,
            order_source_mode=args.order_source_mode,
            seed=args.seed,
            arrival_rate_per_min=args.arrival_rate_per_min,
            deterministic=not args.stochastic,
            on_reset=_on_reset,
            on_step=_on_step,
        )
        renderer.hold(args.hold_final_sec)
    finally:
        renderer.close()

    print(
        json.dumps(
            {
                "policy_path": str(policy_path),
                "checkpoint_meta": runtime.checkpoint_meta,
                "order_source_summary": rollout["order_source_summary"],
                "episode_metrics": rollout["episode_metrics"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _resolve_policy_path(*, policy_path: str, policy_dir: str) -> Path:
    if policy_path:
        resolved = Path(policy_path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"policy.pt 不存在: {resolved}")
        return resolved
    if policy_dir:
        resolved = Path(policy_dir).resolve() / "policy.pt"
        if not resolved.is_file():
            raise FileNotFoundError(f"policy.pt 不存在: {resolved}")
        return resolved

    weights_dir = REPO_ROOT / "backend" / "weights" / "rh_alns_cmrappo"
    candidates = sorted(
        (path / "policy.pt" for path in weights_dir.glob("phase7_*") if (path / "policy.pt").is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"未在 {weights_dir} 下找到任何 policy.pt")
    return candidates[0]


if __name__ == "__main__":
    raise SystemExit(main())
