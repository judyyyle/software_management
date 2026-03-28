#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HiveLogix — 主 Flask 应用入口

架构说明：
  - 各功能模块以 Blueprint 形式注册，挂载在独立 URL 前缀下
  - Geo 模块：  /api/geo/*
  - 未来扩展：  /api/dispatch/*, /api/orders/*, /api/fleet/*, ...

启动方式：
  cd backend
  python app.py          → http://localhost:8000

独立调试 Geo 模块：
  cd backend/environment/geo
  python app.py          → http://localhost:5000
"""

import os
import sys

from flask import Flask, jsonify
from flask_cors import CORS

# ── 模块路径注入（保持子模块内部的平坦式 import 不变）─────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GEO_DIR  = os.path.join(BASE_DIR, "environment", "geo")
sys.path.insert(0, GEO_DIR)

# ── Blueprint 导入（在 sys.path 注入之后）─────────────────────────────────────
from geo_blueprint import geo_bp           # noqa: E402
from data_loader   import load_shapefile_async  # noqa: E402

# ── 应用初始化 ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

CORS(app, resources={r"/api/*": {
    "origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
}})

# ── Blueprint 注册 ─────────────────────────────────────────────────────────────
app.register_blueprint(geo_bp, url_prefix="/api/geo")

# 扩展示例（待实现）：
# from api.routes.dispatch import dispatch_bp
# app.register_blueprint(dispatch_bp, url_prefix="/api/dispatch")


# ── 通用端点 ───────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health_check():
    """服务健康探针。"""
    return jsonify({"status": "ok", "service": "HiveLogix"})


# ── 入口 ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    load_shapefile_async()
    print("[INFO] HiveLogix 主服务启动: http://localhost:8000")
    print("[INFO] Geo API:   http://localhost:8000/api/geo/status")
    print("[INFO] 健康检查:  http://localhost:8000/api/health")
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)
