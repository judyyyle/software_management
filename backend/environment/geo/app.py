#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UAV 禁飞区地图 — 独立运行入口

仅用于直接 `python app.py` 单模块调试（端口 5000）。
生产/集成环境请使用主入口 backend/app.py（端口 8000）。

所有路由逻辑已迁移至 geo_blueprint.py；本文件仅负责：
  - 创建 Flask 应用实例
  - 挂载 Blueprint（prefix=/api，兼容旧路径）
  - 提供 Legacy HTML 页面入口
"""

import os

from flask import Flask, render_template
from flask_cors import CORS

from data_loader   import load_shapefile_async
from geo_blueprint import geo_bp

# ── 应用配置 ──────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", template_folder="templates")
# 独立运行时保持原有 /api/* 路径，兼容直连调试
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:5173", "http://127.0.0.1:5173"]}})


# ── Blueprint 挂载（独立运行模式：前缀 /api，路径与旧版兼容）──────────────────
app.register_blueprint(geo_bp, url_prefix="/api")


# ── Legacy HTML 入口（用于直接访问 Flask 模板页，集成模式下不使用）───────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    load_shapefile_async()
    print("[INFO] 服务器启动: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
