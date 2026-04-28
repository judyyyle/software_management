#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 基于 SUMO GUI 的训练/验证实时可视化。
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from training.export_sumo_truck_route import export_phase4_truck_route
from training.sumo_net_normalizer import normalize_net_with_netconvert, resolve_netconvert
from training.scene_loader import DEFAULT_CONFIG_PATH, load_default_scene


class RealtimeSumoEpisodeRenderer:
    """将训练/验证 episode 的订单与无人机状态叠加到 SUMO GUI。"""

    def __init__(
        self,
        *,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        sumo_gui_bin: str | None = None,
        gui_step_sec: float = 5.0,
        playback_speed: float = 20.0,
        zoom: float = 900.0,
    ) -> None:
        if gui_step_sec <= 0:
            raise ValueError("gui_step_sec 必须为正数")
        if playback_speed <= 0:
            raise ValueError("playback_speed 必须为正数")

        self._config_path = Path(config_path)
        self._scene_ctx = load_default_scene(config_path=self._config_path)
        export_result = export_phase4_truck_route(config_path=self._config_path)
        self._truck_id = str(export_result.execution_route.truck_id)

        self._sumo_gui_bin = _resolve_sumo_gui(sumo_gui_bin)
        self._traci = _import_sumo_module("traci", self._sumo_gui_bin)
        self._sumolib = _import_sumo_module("sumolib", self._sumo_gui_bin)

        self._route_dir = Path(self._scene_ctx.scene_bundle_dir) / "sumo" / "phase4_truck_route"
        self._sumocfg = self._route_dir / "truck_route.sumocfg"
        self._net_path = self._route_dir / "truck_route.net.xml"
        if not self._sumocfg.is_file():
            raise FileNotFoundError(f"SUMO 配置不存在: {self._sumocfg}")
        if not self._net_path.is_file():
            raise FileNotFoundError(f"SUMO 路网不存在: {self._net_path}")
        normalize_net_with_netconvert(
            sumocfg=self._sumocfg,
            netconvert_bin=resolve_netconvert(self._sumo_gui_bin),
        )

        net = self._sumolib.net.readNet(str(self._net_path))
        offset_x, offset_y = net.getLocationOffset()
        self._offset_x = float(offset_x)
        self._offset_y = float(offset_y)

        self._gui_step_sec = float(gui_step_sec)
        self._playback_speed = float(playback_speed)
        self._zoom = float(zoom)
        self._label = f"cmrappo-live-{os.getpid()}-{id(self)}"
        self._command = self._build_command()
        self._load_args = self._command[1:]
        self._started = False
        self._view_id: str | None = None
        self._last_sim_time = 0.0
        self._live_order_ids: set[str] = set()
        self._live_drone_ids: set[str] = set()

    def reset_episode(self) -> None:
        if not self._started:
            self._traci.start(self._command, label=self._label)
            self._started = True
        else:
            self._traci.switch(self._label)
            self._traci.load(self._load_args)
        self._view_id = self._resolve_view_id()
        if self._view_id is not None:
            try:
                self._traci.gui.trackVehicle(self._view_id, self._truck_id)
                self._traci.gui.setZoom(self._view_id, self._zoom)
            except Exception:
                pass
        self._last_sim_time = 0.0
        self._live_order_ids.clear()
        self._live_drone_ids.clear()

    def sync_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        if not self._started:
            self.reset_episode()
        self._traci.switch(self._label)

        target_time = max(0.0, float(snapshot.get("t_now", 0.0)))
        if target_time + 1e-6 < self._last_sim_time:
            self.reset_episode()
        self._advance_to(target_time)

        current_decision = snapshot.get("current_decision") or {}
        deciding_drone_id = (
            None
            if not isinstance(current_decision, Mapping)
            else current_decision.get("drone_id")
        )
        self._sync_orders(snapshot.get("orders", ()), t_now=target_time)
        self._sync_drones(
            snapshot.get("drones", ()),
            deciding_drone_id=None if deciding_drone_id is None else str(deciding_drone_id),
        )

    def hold(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(float(seconds))

    def close(self) -> None:
        if not self._started:
            return
        self._traci.switch(self._label)
        self._traci.close(wait=True)
        self._started = False
        self._view_id = None

    def _build_command(self) -> list[str]:
        command = [
            self._sumo_gui_bin,
            "-c",
            str(self._sumocfg),
            "--start",
            "--delay",
            "50",
            "--step-length",
            "1.0",
            "--disable-textures",
        ]
        gui_settings = self._route_dir / "phase4_gui.view.xml"
        if gui_settings.is_file():
            command.extend(["--gui-settings-file", str(gui_settings)])
        return command

    def _resolve_view_id(self) -> str | None:
        self._traci.switch(self._label)
        view_ids = list(self._traci.gui.getIDList())
        return str(view_ids[0]) if view_ids else None

    def _advance_to(self, target_time: float) -> None:
        self._traci.switch(self._label)
        sim_time = float(self._traci.simulation.getTime())
        while sim_time + 1e-6 < target_time:
            next_time = min(target_time, sim_time + self._gui_step_sec)
            self._traci.simulationStep(next_time)
            delta = max(0.0, next_time - sim_time)
            sleep_sec = delta / max(self._playback_speed, 1e-6)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            sim_time = float(self._traci.simulation.getTime())
        self._last_sim_time = float(target_time)

    def _sync_orders(self, orders: Sequence[Mapping[str, Any]] | Any, *, t_now: float) -> None:
        active_ids: set[str] = set()
        if not isinstance(orders, Sequence):
            orders = ()
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            order_id = str(order.get("order_id", "")).strip()
            if not order_id:
                continue
            active_ids.add(order_id)
            x, y = self._to_sumo_xy(order)
            status = str(order.get("status", "PENDING"))
            color = _order_color(status)
            label = (
                f"{order_id} | {status} | ddl={float(order.get('deadline', t_now)) - t_now:.0f}s"
            )
            self._upsert_marker(
                namespace="order",
                object_id=order_id,
                x=x,
                y=y,
                color=color,
                radius=26.0 if status in {"PENDING", "ASSIGNED"} else 20.0,
                label=label,
            )

        for stale_order_id in self._live_order_ids - active_ids:
            self._remove_marker("order", stale_order_id)
        self._live_order_ids = active_ids

    def _sync_drones(
        self,
        drones: Sequence[Mapping[str, Any]] | Any,
        *,
        deciding_drone_id: str | None,
    ) -> None:
        active_ids: set[str] = set()
        if not isinstance(drones, Sequence):
            drones = ()
        for drone in drones:
            if not isinstance(drone, Mapping):
                continue
            drone_id = str(drone.get("drone_id", "")).strip()
            if not drone_id:
                continue
            active_ids.add(drone_id)
            x, y = self._to_sumo_xy(drone)
            status = str(drone.get("status", "idle"))
            color = _drone_color(
                status=status,
                is_deciding=(drone_id == deciding_drone_id),
            )
            battery_ratio = float(drone.get("battery_ratio", 0.0))
            carrying_order_id = drone.get("carrying_order_id")
            suffix = "" if not carrying_order_id else f" | {carrying_order_id}"
            label = f"{drone_id} | {status} | {battery_ratio:.0%}{suffix}"
            self._upsert_marker(
                namespace="drone",
                object_id=drone_id,
                x=x,
                y=y,
                color=color,
                radius=18.0,
                label=label,
            )

        for stale_drone_id in self._live_drone_ids - active_ids:
            self._remove_marker("drone", stale_drone_id)
        self._live_drone_ids = active_ids

    def _upsert_marker(
        self,
        *,
        namespace: str,
        object_id: str,
        x: float,
        y: float,
        color: tuple[int, int, int, int],
        radius: float,
        label: str,
    ) -> None:
        self._traci.switch(self._label)
        poi_id = _marker_id(namespace, object_id, "poi")
        polygon_id = _marker_id(namespace, object_id, "poly")
        shape = _circle_shape(x=x, y=y, radius=radius)

        try:
            self._traci.poi.add(
                poi_id,
                x,
                y,
                color,
                poiType=namespace,
                layer=20,
            )
            self._traci.poi.setWidth(poi_id, max(10.0, radius * 0.9))
            self._traci.poi.setHeight(poi_id, max(10.0, radius * 0.9))
        except Exception:
            self._traci.poi.setPosition(poi_id, x, y)
            self._traci.poi.setColor(poi_id, color)
            self._traci.poi.setType(poi_id, namespace)
            self._traci.poi.setWidth(poi_id, max(10.0, radius * 0.9))
            self._traci.poi.setHeight(poi_id, max(10.0, radius * 0.9))
        self._traci.poi.setParameter(poi_id, "PARAM_TEXT", label)

        try:
            self._traci.polygon.add(
                polygon_id,
                shape,
                color,
                fill=True,
                polygonType=namespace,
                layer=19,
                lineWidth=2,
            )
        except Exception:
            self._traci.polygon.setShape(polygon_id, shape)
            self._traci.polygon.setColor(polygon_id, color)

    def _remove_marker(self, namespace: str, object_id: str) -> None:
        self._traci.switch(self._label)
        poi_id = _marker_id(namespace, object_id, "poi")
        polygon_id = _marker_id(namespace, object_id, "poly")
        try:
            self._traci.poi.remove(poi_id)
        except Exception:
            pass
        try:
            self._traci.polygon.remove(polygon_id)
        except Exception:
            pass

    def _to_sumo_xy(self, payload: Mapping[str, Any]) -> tuple[float, float]:
        x = float(payload["x"]) + self._offset_x
        y = float(payload["y"]) + self._offset_y
        return x, y


def _resolve_sumo_gui(binary_override: str | None) -> str:
    if binary_override:
        return str(binary_override)
    found = shutil.which("sumo-gui")
    if found:
        return found
    raise FileNotFoundError(
        "未找到 `sumo-gui`。请先安装 SUMO，或通过 `--sumo-gui-bin` 指定路径。"
    )


def _import_sumo_module(module_name: str, sumo_gui_bin: str):
    try:
        return __import__(module_name)
    except ImportError:
        for tools_dir in _candidate_sumo_tools_dirs(sumo_gui_bin):
            if str(tools_dir) not in sys.path:
                sys.path.insert(0, str(tools_dir))
            try:
                return __import__(module_name)
            except ImportError:
                continue
    raise ImportError(
        f"无法导入 {module_name}。请确认已安装 SUMO，并设置 `SUMO_HOME` 或可访问其 tools 目录。"
    )


def _candidate_sumo_tools_dirs(sumo_gui_bin: str) -> tuple[Path, ...]:
    gui_path = Path(sumo_gui_bin).resolve()
    candidates: list[Path] = []
    sumo_home = os.environ.get("SUMO_HOME")
    if sumo_home:
        candidates.append(Path(sumo_home) / "tools")
    candidates.extend(
        [
            gui_path.parent / "tools",
            gui_path.parent.parent / "share" / "sumo" / "tools",
            Path("/usr/share/sumo/tools"),
            Path("/opt/homebrew/share/sumo/tools"),
            Path("/Library/Frameworks/EclipseSUMO.framework/Versions/Current/EclipseSUMO/share/sumo/tools"),
            Path("/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/share/sumo/tools"),
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            unique.append(path)
    return tuple(unique)


def _marker_id(namespace: str, object_id: str, suffix: str) -> str:
    return f"cmrappo-{namespace}-{object_id}-{suffix}"


def _circle_shape(*, x: float, y: float, radius: float, points: int = 14) -> list[tuple[float, float]]:
    return [
        (
            x + radius * math.cos((2.0 * math.pi * idx) / points),
            y + radius * math.sin((2.0 * math.pi * idx) / points),
        )
        for idx in range(points)
    ]


def _order_color(status: str) -> tuple[int, int, int, int]:
    normalized = status.upper()
    if normalized == "PENDING":
        return (235, 64, 52, 230)
    if normalized == "ASSIGNED":
        return (255, 158, 27, 230)
    if normalized == "COMPLETED":
        return (38, 170, 76, 220)
    if normalized == "TIMEOUT":
        return (110, 110, 110, 220)
    return (180, 70, 70, 220)


def _drone_color(*, status: str, is_deciding: bool) -> tuple[int, int, int, int]:
    if is_deciding:
        return (255, 215, 0, 245)
    normalized = status.lower()
    if "failure" in normalized or "fallback" in normalized:
        return (170, 32, 32, 235)
    if "charging" in normalized or "queue" in normalized:
        return (255, 140, 0, 235)
    if "flying" in normalized or "return" in normalized:
        return (0, 180, 220, 235)
    return (32, 110, 235, 235)
