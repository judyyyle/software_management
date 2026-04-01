#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 实体管理器 (Section 2.1)

EntityManager 是后端后端 Single Source of Truth 的实体容器层，持有并驱动所有
Depot / SwapStation / Truck / Drone 实例，并维护前端 UI 字段的 Sidecar 元数据。

生命周期：
  1. POST /api/sim/init   → load_from_config(config_json) 重建所有实体
  2. SimEngine 每 100ms  → tick_all(current_time, dt)     物理步进
  3. WebSocket 推送      → get_telemetry()                TICK 帧数据
  4. 建连/请求           → get_static_snapshot()          FULL_SNAPSHOT 数据

导入规则（依赖 app.py 已将 BASE_DIR 注入 sys.path）：
  from core.entities.xxx import Xxx
  from utils.coord_utils import wgs84_to_utm
"""

from __future__ import annotations

import logging
from typing import Optional

from core.entities.depot import Depot
from core.entities.drone import Drone, HeavyDrone, LightDrone
from core.entities.primitives import Position3D, SourceType
from core.entities.swap_station import SwapStation
from core.entities.truck import Truck
from utils.coord_utils import wgs84_to_utm

logger = logging.getLogger(__name__)

# 无人机类型名称 → 类映射
_DRONE_CLASS_MAP: dict[str, type[Drone]] = {
    "LightDrone": LightDrone,
    "HeavyDrone": HeavyDrone,
}


class EntityManager:
    """
    全局实体容器。

    存储结构：
      depots   : {depot_id:   Depot}
      stations : {station_id: SwapStation}
      trucks   : {truck_id:   Truck}
      drones   : {drone_id:   Drone}
      _metadata: {entity_id: {name, type, [home_depot_id]}}
                 存储 core.entities 构造函数不接受的 UI 字段
    """

    def __init__(self) -> None:
        self.depots:   dict[str, Depot]       = {}
        self.stations: dict[str, SwapStation] = {}
        self.trucks:   dict[str, Truck]       = {}
        self.drones:   dict[str, Drone]       = {}
        self._metadata: dict[str, dict]       = {}

    # ══════════════════════════════════════════════════════════════════════════
    # 初始化
    # ══════════════════════════════════════════════════════════════════════════

    def load_from_config(self, config_json: dict) -> None:
        """
        从 /api/sim/init 请求体中的 entities 字段实例化所有实体。

        必须严格按四步顺序执行（详见 Section 2.1 设计文档）：
          1. 基础设施（Depot / SwapStation）
          2a. Truck（先于 Drone，供后者读取初始坐标）
          2b. Drone（按 home_type 从 Depot/Truck 取初始坐标）
          3. 关联注册（register_drone / docked_drones.append / register_truck）
          4. Sidecar 元数据填充

        Args:
            config_json: /api/sim/init 请求体（包含 entities 子字典）
        """
        self.depots.clear()
        self.stations.clear()
        self.trucks.clear()
        self.drones.clear()
        self._metadata.clear()

        entities = config_json.get("entities", {})

        # ── 步骤 1：实例化基础设施 ───────────────────────────────────────────
        for d_cfg in entities.get("depots", []):
            x, y = wgs84_to_utm(d_cfg["lng"], d_cfg["lat"])
            loc   = Position3D(x=x, y=y, z=float(d_cfg.get("altitude", 0)))
            depot = Depot(
                depot_id=d_cfg["depot_id"],
                location=loc,
                swap_time=float(d_cfg["swap_time"]),
                parking_slots=int(d_cfg["parking_slots"]),
                capacity=int(d_cfg.get("capacity", 1000)),
            )
            self.depots[d_cfg["depot_id"]] = depot
            logger.debug("[EntityManager] 创建 Depot %s", d_cfg["depot_id"])

        for s_cfg in entities.get("stations", []):
            x, y = wgs84_to_utm(s_cfg["lng"], s_cfg["lat"])
            loc     = Position3D(x=x, y=y, z=float(s_cfg.get("altitude", 0)))
            station = SwapStation(
                station_id=s_cfg["station_id"],
                location=loc,
                swap_time=float(s_cfg["swap_time"]),
                parking_slots=int(s_cfg["parking_slots"]),
            )
            self.stations[s_cfg["station_id"]] = station
            logger.debug("[EntityManager] 创建 SwapStation %s", s_cfg["station_id"])

        # ── 步骤 2a：实例化 Truck（必须先于 Drone）──────────────────────────
        for t_cfg in entities.get("trucks", []):
            home_depot_id = t_cfg["home_depot_id"]
            if home_depot_id not in self.depots:
                raise ValueError(
                    f"Truck {t_cfg['truck_id']} 引用的 home_depot_id '{home_depot_id}' 不存在，"
                    "请检查 entities.depots 配置。"
                )
            depot_loc = self.depots[home_depot_id].location
            init_loc  = Position3D(x=depot_loc.x, y=depot_loc.y, z=0.0)
            truck = Truck(
                truck_id=t_cfg["truck_id"],
                speed=float(t_cfg["speed"]),
                max_inventory=int(t_cfg["max_inventory"]),
                swap_time=float(t_cfg["swap_time"]),
                parking_slots=int(t_cfg["parking_slots"]),
                init_loc=init_loc,
            )
            self.trucks[t_cfg["truck_id"]] = truck
            logger.debug("[EntityManager] 创建 Truck %s", t_cfg["truck_id"])

        # ── 步骤 2b：实例化 Drone（Truck 已就绪）────────────────────────────
        for dr_cfg in entities.get("drones", []):
            drone_type_name = dr_cfg.get("drone_type", "LightDrone")
            drone_class = _DRONE_CLASS_MAP.get(drone_type_name)
            if drone_class is None:
                raise ValueError(
                    f"未知无人机类型 '{drone_type_name}'，"
                    f"支持: {list(_DRONE_CLASS_MAP.keys())}"
                )
            home_id   = dr_cfg["home_id"]
            home_type = dr_cfg["home_type"]   # "DEPOT" | "TRUCK"

            if home_type == "DEPOT":
                if home_id not in self.depots:
                    raise ValueError(
                        f"Drone {dr_cfg['drone_id']} 的 home_id '{home_id}' 不在 depots 中。"
                    )
                init_loc = self.depots[home_id].location
            elif home_type == "TRUCK":
                if home_id not in self.trucks:
                    raise ValueError(
                        f"Drone {dr_cfg['drone_id']} 的 home_id '{home_id}' 不在 trucks 中。"
                    )
                init_loc = self.trucks[home_id].current_loc
            else:
                raise ValueError(
                    f"Drone {dr_cfg['drone_id']} 的 home_type '{home_type}' 无效，"
                    "必须为 'DEPOT' 或 'TRUCK'。"
                )

            drone = drone_class(
                drone_id=dr_cfg["drone_id"],
                home_id=home_id,
                home_type=SourceType(home_type),
                init_loc=init_loc,
            )
            self.drones[dr_cfg["drone_id"]] = drone
            logger.debug("[EntityManager] 创建 %s %s", drone_type_name, dr_cfg["drone_id"])

        # ── 步骤 3：关联注册 ─────────────────────────────────────────────────
        for dr_cfg in entities.get("drones", []):
            drone_id  = dr_cfg["drone_id"]
            home_id   = dr_cfg["home_id"]
            home_type = dr_cfg["home_type"]
            if home_type == "DEPOT":
                self.depots[home_id].register_drone(drone_id, is_idle=True)
            else:  # TRUCK
                if drone_id not in self.trucks[home_id].docked_drones:
                    self.trucks[home_id].docked_drones.append(drone_id)

        for t_cfg in entities.get("trucks", []):
            self.depots[t_cfg["home_depot_id"]].register_truck(t_cfg["truck_id"])

        # ── 步骤 4：填充 Sidecar 元数据 ──────────────────────────────────────
        for d_cfg in entities.get("depots", []):
            self._metadata[d_cfg["depot_id"]] = {
                "name": d_cfg.get("name", d_cfg["depot_id"]),
                "type": "DEPOT",
            }
        for s_cfg in entities.get("stations", []):
            self._metadata[s_cfg["station_id"]] = {
                "name": s_cfg.get("name", s_cfg["station_id"]),
                "type": "STATION",
            }
        for t_cfg in entities.get("trucks", []):
            self._metadata[t_cfg["truck_id"]] = {
                "name":          t_cfg.get("name", t_cfg["truck_id"]),
                "type":          "TRUCK",
                "home_depot_id": t_cfg["home_depot_id"],
            }
        for dr_cfg in entities.get("drones", []):
            self._metadata[dr_cfg["drone_id"]] = {
                "type": "DRONE",
                # home_id / home_type 已在 Drone 实例构造函数中存储，无需 sidecar
            }

        logger.info(
            "[EntityManager] 加载完成：%d 仓库，%d 换电站，%d 卡车，%d 无人机",
            len(self.depots), len(self.stations), len(self.trucks), len(self.drones),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 快照序列化
    # ══════════════════════════════════════════════════════════════════════════

    def get_static_snapshot(self) -> dict:
        """
        返回所有实体的完整静态元数据 + 运行时状态，用于 FULL_SNAPSHOT 首帧。

        实现：对每个实体调用 to_telemetry_dict()，再与 _metadata 合并，
        使 name / home_depot_id 等 UI 字段出现在输出中。

        Returns:
            dict with keys: depots, stations, trucks, drones（各为列表）
        """
        return {
            "depots": [
                {**depot.to_telemetry_dict(), **self._metadata.get(did, {})}
                for did, depot in self.depots.items()
            ],
            "stations": [
                {**station.to_telemetry_dict(), **self._metadata.get(sid, {})}
                for sid, station in self.stations.items()
            ],
            "trucks": [
                {**truck.to_telemetry_dict(), **self._metadata.get(tid, {})}
                for tid, truck in self.trucks.items()
            ],
            "drones": [
                {**drone.to_telemetry_dict(), **self._metadata.get(did, {})}
                for did, drone in self.drones.items()
            ],
        }

    def get_telemetry(self) -> dict:
        """
        返回所有实体的动态运行时字段，用于 TICK 帧（100ms 广播）。

        实现：对每个实体调用 to_dynamic_state()，再与 _metadata 合并，
        确保 TruckConfig.name / home_depot_id 等非 Optional TypeScript 字段
        不因 setRuntimeAll 全量替换而变成 undefined。（v4.9 修正）

        Returns:
            dict with keys: depots, stations, trucks, drones（各为列表）
        """
        return {
            "depots": [
                {**depot.to_dynamic_state(), **self._metadata.get(did, {})}
                for did, depot in self.depots.items()
            ],
            "stations": [
                {**station.to_dynamic_state(), **self._metadata.get(sid, {})}
                for sid, station in self.stations.items()
            ],
            "trucks": [
                {**truck.to_dynamic_state(), **self._metadata.get(tid, {})}
                for tid, truck in self.trucks.items()
            ],
            "drones": [
                {**drone.to_dynamic_state(), **self._metadata.get(did, {})}
                for did, drone in self.drones.items()
            ],
        }

    # ══════════════════════════════════════════════════════════════════════════
    # 物理步进
    # ══════════════════════════════════════════════════════════════════════════

    def tick_all(self, current_time: float, dt: float) -> None:
        """
        驱动所有实体完成一个物理时间步。

        仅调用已在各实体类中实现的 tick_update()。
        无人机与卡车的主动运动逻辑（move_step / consume_energy）由调度引擎
        在 Phase 4 接管后注入；当前阶段各实体停止在初始位置，仅基础设施充换电队列推进。

        Args:
            current_time: 仿真累计时间（秒）
            dt:           本步推进的仿真时长（秒）= 0.1 × speed_ratio
        """
        for depot in self.depots.values():
            try:
                depot.tick_update(current_time)
            except Exception:
                logger.exception("[EntityManager.tick_all] Depot %s tick 异常", depot.depot_id)

        for station in self.stations.values():
            try:
                station.tick_update(current_time)
            except Exception:
                logger.exception("[EntityManager.tick_all] Station %s tick 异常", station.station_id)

        # 卡车提供了 tick_update（继承自 ChargingHost），无人机暂无独立 tick
        for truck in self.trucks.values():
            try:
                truck.tick_update(current_time)
            except Exception:
                logger.exception("[EntityManager.tick_all] Truck %s tick 异常", truck.truck_id)
