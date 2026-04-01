#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 仿真 API Blueprint (Section 3 + 4)

提供仿真控制 REST 接口：
  POST /api/sim/init      接收实体清单 + 订单生成参数，初始化所有 Manager
  POST /api/sim/control   start / pause / reset / set speed
  GET  /api/sim/state     返回完整快照（供 F5 刷新/重连恢复）
  GET  /api/sim/orders    返回最近 N 条订单（供 OrderTask 页面列表展示）

以及 WebSocket 端点：
  WS   /api/ws/telemetry  通过 telemetry.register_route(sock) 注册

flask-sock 实例 sock 在此模块创建后由 app.py 调用 sock.init_app(app) 绑定。

模块级单例：
  _entity_mgr : EntityManager
  _order_mgr  : OrderManager
  _sim_engine : SimulationEngine
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request
from flask_sock import Sock

from api.websockets.telemetry import broadcast_tick, register_route, set_snapshot_builder
from entity_manager import EntityManager
from order_manager import OrderManager
from sim_engine import SimulationEngine

logger = logging.getLogger(__name__)

sim_bp = Blueprint("sim", __name__)
sock   = Sock()  # app.py 调用 sock.init_app(app) 绑定 Flask 应用

# ── 模块级单例（由 /api/sim/init 初始化）─────────────────────────────────────
_entity_mgr: EntityManager   = EntityManager()
_order_mgr:  OrderManager    = OrderManager()
_sim_engine: SimulationEngine = SimulationEngine()

# 注册 WebSocket 遥测端点，并将 sim_engine 的完整快照构造函数注入 telemetry 模块
register_route(sock)
set_snapshot_builder(_sim_engine.build_full_snapshot)


# ══════════════════════════════════════════════════════════════════════════════
# POST /init
# ══════════════════════════════════════════════════════════════════════════════

@sim_bp.route("/init", methods=["POST"])
def sim_init():
    """
    接收实体清单（entities）+ 订单生成参数（order_gen_config）+ 地图边界（bbox）。

    重置所有 Manager，准备就绪后返回加载汇总。

    请求体 JSON 格式（详见 Section 3 文档示例）：
    {
      "scene_id": "...",
      "bbox": {"min_lng": ..., "min_lat": ..., "max_lng": ..., "max_lat": ...},
      "entities": { "depots": [...], "stations": [...], "trucks": [...], "drones": [...] },
      "order_gen_config": { "arrival_rate": 4, ... }
    }
    """
    global _entity_mgr, _order_mgr, _sim_engine

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "请求体必须为 JSON"}), 400

    # ── 输入校验 ─────────────────────────────────────────────────────────────
    entities    = body.get("entities")
    bbox        = body.get("bbox")
    gen_config  = body.get("order_gen_config", {})

    if not entities:
        return jsonify({"error": "缺少 entities 字段"}), 400
    if not bbox or not all(k in bbox for k in ("min_lng", "min_lat", "max_lng", "max_lat")):
        return jsonify({"error": "缺少或格式错误的 bbox 字段"}), 400

    # ── 重置并重新初始化 ─────────────────────────────────────────────────────
    try:
        _sim_engine.reset()

        _entity_mgr = EntityManager()
        _order_mgr  = OrderManager()
        _sim_engine = SimulationEngine()

        _entity_mgr.load_from_config(body)
        _order_mgr.configure(gen_config, bbox)
        _sim_engine.attach(_entity_mgr, _order_mgr)

        # 更新 telemetry 快照构造函数绑定（新 _sim_engine 实例）
        set_snapshot_builder(_sim_engine.build_full_snapshot)

        logger.info(
            "[sim_init] 初始化完成：scene_id=%s，%d 仓库，%d 换电站，%d 卡车，%d 无人机",
            body.get("scene_id", "unknown"),
            len(_entity_mgr.depots),
            len(_entity_mgr.stations),
            len(_entity_mgr.trucks),
            len(_entity_mgr.drones),
        )
    except (KeyError, ValueError) as exc:
        logger.exception("[sim_init] 初始化失败")
        return jsonify({"error": str(exc)}), 422

    return jsonify({
        "status":  "initialized",
        "summary": {
            "depots":   len(_entity_mgr.depots),
            "stations": len(_entity_mgr.stations),
            "trucks":   len(_entity_mgr.trucks),
            "drones":   len(_entity_mgr.drones),
        },
    })


# ══════════════════════════════════════════════════════════════════════════════
# POST /control
# ══════════════════════════════════════════════════════════════════════════════

@sim_bp.route("/control", methods=["POST"])
def sim_control():
    """
    控制仿真启停与速率。

    请求体：
      {"action": "start" | "pause" | "reset", "speed": <float>}

    Returns:
      JSON {"status": "ok", "is_running": bool, "sim_time": float, "speed_ratio": float}
    """
    body   = request.get_json(silent=True) or {}
    action = body.get("action", "")
    speed  = body.get("speed")

    try:
        if action == "start":
            if not _sim_engine.is_running:
                _sim_engine.start()
        elif action == "pause":
            _sim_engine.pause()
        elif action == "reset":
            _sim_engine.reset()
        elif action == "set_speed":
            pass  # 仅调整速率，不改变运行状态
        else:
            return jsonify({"error": f"未知 action: '{action}'，支持 start/pause/reset/set_speed"}), 400

        if speed is not None:
            _sim_engine.set_speed(float(speed))

    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 409

    return jsonify({
        "status":      "ok",
        "is_running":  _sim_engine.is_running,
        "sim_time":    round(_sim_engine.current_time, 3),
        "speed_ratio": _sim_engine.speed_ratio,
    })


# ══════════════════════════════════════════════════════════════════════════════
# GET /state
# ══════════════════════════════════════════════════════════════════════════════

@sim_bp.route("/state", methods=["GET"])
def sim_state():
    """
    返回后端当前完整快照，供 F5 刷新 / 重新连接时恢复前端状态。

    响应结构与 WebSocket FULL_SNAPSHOT payload 完全对齐，
    但不包含 type 字段（直接返回 payload 内容）。
    """
    snapshot = _sim_engine.build_full_snapshot()
    return jsonify(snapshot["payload"])


# ══════════════════════════════════════════════════════════════════════════════
# GET /orders
# ══════════════════════════════════════════════════════════════════════════════

@sim_bp.route("/orders", methods=["GET"])
def sim_orders():
    """
    返回最近 N 条订单详情，供 OrderTask 页面列表展示。

    Query params:
      limit (int, default=100): 最多返回条数
    """
    try:
        limit = int(request.args.get("limit", 100))
        limit = max(1, min(limit, 1000))   # 防范滥用，限[1, 1000]
    except (TypeError, ValueError):
        limit = 100

    orders = _order_mgr.get_recent_orders(limit)
    return jsonify({
        "total":  len(orders),
        "orders": orders,
    })
