#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在训练主循环中实时将订单、无人机叠加到 SUMO GUI。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from training.scene_loader import DEFAULT_CONFIG_PATH
from training.sumo_live_renderer import RealtimeSumoEpisodeRenderer
from training.train_cmrappo import train_cmrappo


class _LiveTrainObserver:
    def __init__(
        self,
        *,
        renderer: RealtimeSumoEpisodeRenderer,
        render_every_n_episode: int,
        max_rendered_episodes: int,
        hold_final_sec: float,
    ) -> None:
        self._renderer = renderer
        self._render_every_n_episode = max(1, int(render_every_n_episode))
        self._max_rendered_episodes = max(0, int(max_rendered_episodes))
        self._hold_final_sec = max(0.0, float(hold_final_sec))
        self._active_episode_id: int | None = None
        self._rendered_episode_count = 0

    def __call__(self, event_name: str, payload) -> None:
        if event_name == "train_reset":
            episode_id = int(payload["episode_id"])
            should_render = (episode_id % self._render_every_n_episode) == 0
            if self._max_rendered_episodes > 0 and self._rendered_episode_count >= self._max_rendered_episodes:
                should_render = False
            self._active_episode_id = episode_id if should_render else None
            if not should_render:
                return
            self._rendered_episode_count += 1
            env = payload["env"]
            self._renderer.reset_episode()
            self._renderer.sync_snapshot(env.build_visualization_snapshot())
            print(
                f"[live-train] render episode={episode_id} "
                f"global_step={int(payload['global_step'])} update={int(payload['update_idx'])}"
            )
            return

        if self._active_episode_id is None:
            return
        if int(payload["episode_id"]) != self._active_episode_id:
            return

        if event_name == "train_step":
            env = payload["env"]
            self._renderer.sync_snapshot(env.build_visualization_snapshot())
            return

        if event_name == "train_episode_end":
            metrics = payload["episode_metrics"]
            print(
                "[live-train] episode_end "
                f"episode={self._active_episode_id} "
                f"reward={float(metrics['total_reward']):.3f} "
                f"deliveries={int(metrics['delivery_count'])} "
                f"on_time_rate={float(metrics['on_time_rate']):.3f}"
            )
            self._renderer.hold(self._hold_final_sec)
            self._active_episode_id = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="训练时实时 SUMO 可视化")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="训练输出目录；为空时自动按时间戳创建到 backend/weights/rh_alns_cmrappo 下",
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
        "--gui-step-sec",
        type=float,
        default=5.0,
        help="GUI 每次推进的仿真秒数",
    )
    parser.add_argument(
        "--playback-speed",
        type=float,
        default=30.0,
        help="播放倍速；越大训练阻塞越少",
    )
    parser.add_argument(
        "--render-every-n-episode",
        type=int,
        default=10,
        help="每隔多少个训练 episode 渲染一次",
    )
    parser.add_argument(
        "--max-rendered-episodes",
        type=int,
        default=0,
        help="最多渲染多少个 episode；0 表示不限制",
    )
    parser.add_argument(
        "--hold-final-sec",
        type=float,
        default=2.0,
        help="每个渲染 episode 结束后停留秒数",
    )
    args = parser.parse_args(argv)

    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else (
            REPO_ROOT
            / "backend"
            / "weights"
            / "rh_alns_cmrappo"
            / datetime.now().strftime("phase7_live_%Y%m%d_%H%M%S")
        )
    )

    renderer = RealtimeSumoEpisodeRenderer(
        config_path=args.config_path,
        sumo_gui_bin=args.sumo_gui_bin or None,
        gui_step_sec=args.gui_step_sec,
        playback_speed=args.playback_speed,
    )
    observer = _LiveTrainObserver(
        renderer=renderer,
        render_every_n_episode=args.render_every_n_episode,
        max_rendered_episodes=args.max_rendered_episodes,
        hold_final_sec=args.hold_final_sec,
    )

    try:
        result = train_cmrappo(
            output_dir=output_dir,
            config_path=args.config_path,
            event_hook=observer,
        )
    finally:
        renderer.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
