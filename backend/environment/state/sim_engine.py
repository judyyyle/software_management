#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 仿真引擎 (Section 2.3)

SimulationEngine 统筹仿真时钟、物理步进与 WebSocket 广播节奏。

核心设计：
  - 后台线程固定每 100ms（wall-clock）唤醒一次
  - 每次唤醒推进仿真时间 = 0.1 × speed_ratio 秒
  - 广播频率固定 10fps，不随 speed_ratio 变化（彻底解耦精度与带宽）
  - 通过 telemetry.broadcast_tick() 线程安全地向所有 WS 客户端推送 TICK 帧

外部依赖：
  - entity_manager.EntityManager  → tick_all / get_telemetry
  - order_manager.OrderManager     → tick / update_timeouts / get_status_summary
  - api.websockets.telemetry       → broadcast_tick
"""

from __future__ import annotations

import logging
import time
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from entity_manager import EntityManager
    from order_manager import OrderManager

logger = logging.getLogger(__name__)

# 后台线程真实睡眠间隔（100ms wall-clock）
_PHYSICS_INTERVAL_S: float = 0.1


class SimulationEngine:
    """
    仿真引擎单例。

    Attributes:
        current_time (float): 仿真累计时间（秒）
        is_running (bool):    引擎是否正在运行
        speed_ratio (float):  仿真加速倍率（默认 1.0）
        sim_start_wall_ms (int): 引擎启动时的 wall-clock 毫秒时间戳
    """

    def __init__(self) -> None:
        self.current_time:      float = 0.0
        self.is_running:        bool  = False
        self.speed_ratio:       float = 1.0
        self.sim_start_wall_ms: int   = 0

        self._entity_mgr: Optional["EntityManager"] = None
        self._order_mgr:  Optional["OrderManager"]  = None
        self._thread:     Optional[threading.Thread] = None
        self._stop_event: threading.Event            = threading.Event()

    # ══════════════════════════════════════════════════════════════════════════
    # 初始化注入
    # ══════════════════════════════════════════════════════════════════════════

    def attach(
        self,
        entity_mgr: "EntityManager",
        order_mgr: "OrderManager",
    ) -> None:
        """
        注入 EntityManager 与 OrderManager（在 /api/sim/init 后调用）。

        Args:
            entity_mgr: 已完成 load_from_config() 的 EntityManager
            order_mgr:  已完成 configure() 的 OrderManager
        """
        self._entity_mgr = entity_mgr
        self._order_mgr  = order_mgr
        logger.info("[SimEngine] 已挂载 EntityManager 和 OrderManager")

    # ══════════════════════════════════════════════════════════════════════════
    # 生命周期控制
    # ══════════════════════════════════════════════════════════════════════════

    def start(self) -> None:
        """
        启动仿真后台线程。

        若已在运行，直接返回。
        """
        if self.is_running:
            logger.warning("[SimEngine] start() 被重复调用，忽略")
            return
        if self._entity_mgr is None or self._order_mgr is None:
            raise RuntimeError("调用 start() 前必须先调用 attach()")

        self._stop_event.clear()
        self.is_running        = True
        self.sim_start_wall_ms = int(time.time() * 1000)

        self._thread = threading.Thread(
            target=self._run_loop,
            name="SimEngine-MainLoop",
            daemon=True,       # 主进程退出时自动终止
        )
        self._thread.start()
        logger.info("[SimEngine] 仿真引擎启动，speed_ratio=%.2f", self.speed_ratio)

    def pause(self) -> None:
        """
        暂停仿真（保留 current_time 与所有实体状态）。
        """
        if not self.is_running:
            return
        self.is_running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("[SimEngine] 仿真引擎已暂停，current_time=%.2f s", self.current_time)

    def reset(self) -> None:
        """
        重置引擎到初始状态（停止后台线程，清空时钟）。

        调用 reset() 后需重新调用 attach() + start() 才能再次运行。
        """
        self.pause()
        self.current_time      = 0.0
        self.sim_start_wall_ms = 0
        self._entity_mgr       = None
        self._order_mgr        = None
        logger.info("[SimEngine] 仿真引擎已重置")

    def set_speed(self, speed_ratio: float) -> None:
        """
        调整仿真加速倍率（线程安全，立即生效）。

        Args:
            speed_ratio: 新倍率，必须 > 0
        """
        if speed_ratio <= 0:
            raise ValueError(f"speed_ratio 必须 > 0，收到: {speed_ratio}")
        self.speed_ratio = speed_ratio
        logger.info("[SimEngine] speed_ratio 调整为 %.2f", self.speed_ratio)

    # ══════════════════════════════════════════════════════════════════════════
    # 主循环（后台线程）
    # ══════════════════════════════════════════════════════════════════════════

    def _run_loop(self) -> None:
        """
        仿真主循环，每 100ms（wall-clock）唤醒一次。

        每次唤醒执行：
          1. 推进仿真时间
          2. 驱动所有实体物理步进
          3. 订单生成 + 超时检测
          4. 广播 TICK 帧
        """
        # 延迟导入避免循环依赖（simulation_bp → sim_engine → telemetry）
        from api.websockets.telemetry import broadcast_tick

        logger.info("[SimEngine._run_loop] 主循环启动")

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            try:
                dt = _PHYSICS_INTERVAL_S * self.speed_ratio

                # ── 1. 推进仿真时间 ─────────────────────────────────────────
                self.current_time += dt

                # ── 2. 物理步进 ─────────────────────────────────────────────
                if self._entity_mgr is not None:
                    self._entity_mgr.tick_all(self.current_time, dt)

                # ── 3. 订单生成 & 超时检测 ──────────────────────────────────
                if self._order_mgr is not None and self._entity_mgr is not None:
                    self._order_mgr.tick(self.current_time, self._entity_mgr)
                    self._order_mgr.update_timeouts(self.current_time)

                # ── 4. 广播 TICK ─────────────────────────────────────────────
                payload = self._build_tick_payload()
                broadcast_tick(payload)

            except Exception:
                logger.exception("[SimEngine._run_loop] 主循环异常，跳过本帧")

            # ── 精确睡眠补偿：保证 100ms wall-clock 间隔 ────────────────────
            elapsed   = time.monotonic() - loop_start
            sleep_sec = max(0.0, _PHYSICS_INTERVAL_S - elapsed)
            if sleep_sec > 0:
                self._stop_event.wait(timeout=sleep_sec)

        logger.info("[SimEngine._run_loop] 主循环退出")

    def _build_tick_payload(self) -> dict:
        """
        构造 TICK 帧的 payload 字典。

        Returns:
            包含 sim_time / entities / stats 的字典
        """
        entities = {}
        stats    = {}

        if self._entity_mgr is not None:
            entities = self._entity_mgr.get_telemetry()

        if self._order_mgr is not None:
            stats = self._order_mgr.get_status_summary()

        return {
            "sim_time": round(self.current_time, 3),
            "entities": entities,
            "stats":    stats,
        }

    def build_full_snapshot(self) -> dict:
        """
        构造 FULL_SNAPSHOT 帧的完整 payload（建连时推送）。

        由 telemetry.set_snapshot_builder() 注入为快照构造函数。

        Returns:
            包含 type="FULL_SNAPSHOT" 的完整消息字典
        """
        entities = {}
        stats    = {}

        if self._entity_mgr is not None:
            entities = self._entity_mgr.get_static_snapshot()

        if self._order_mgr is not None:
            stats = self._order_mgr.get_status_summary()

        return {
            "type": "FULL_SNAPSHOT",
            "payload": {
                "sim_time":          round(self.current_time, 3),
                "is_running":        self.is_running,
                "speed_ratio":       self.speed_ratio,
                "sim_start_wall_ms": self.sim_start_wall_ms,
                "entities":          entities,
                "stats":             stats,
            },
        }
