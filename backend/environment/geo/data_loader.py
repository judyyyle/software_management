#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shapefile 加载与全局状态管理
负责：异步加载 .shp 文件、进度跟踪、高度字段自动检测
"""

import os
import threading
import traceback

import numpy as np
import pandas as pd
import geopandas as gpd

# ── 路径常量 ───────────────────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
SHAPEFILE_PATH = os.path.join(BASE_DIR, "shanghai_map", "shanghai.shp")

# ── 全局状态 ───────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_app_state = {
    "loaded":          False,
    "loading":         False,
    "progress":        0,
    "error":           None,
    "total":           0,
    "columns":         [],
    "numeric_columns": [],
    "height_column":   None,
    "height_stats":    None,
    "bounds":          None,
}
_gdf: gpd.GeoDataFrame = None  # type: ignore

# ── 高度字段检测优先级 ─────────────────────────────────────────────────────────
_HEIGHT_PRIORITY = [
    "height", "Height", "HEIGHT",
    "h", "H",
    "bldg_h", "bldg_height", "building_height", "building_h",
    "elev", "elevation", "ELEV",
    "hgt", "HGT",
    "stories", "floors", "floor", "story",
]
_SKIP_COLS = {
    "fid", "id", "objectid", "osm_id", "gid",
    "area", "perimeter", "shape_area", "shape_len", "shape_leng",
}


def _detect_height_col(gdf: gpd.GeoDataFrame):
    """按优先级从数据列中自动选取高度字段"""
    num_cols = set(gdf.select_dtypes(include=[np.number]).columns)
    for name in _HEIGHT_PRIORITY:
        if name in gdf.columns and name in num_cols:
            return name
    for col in gdf.columns:
        if col in num_cols and any(
            kw in col.lower() for kw in ["height", "elev", "hgt", "floor", "story", "high"]
        ):
            return col
    for col in num_cols:
        if col.lower() not in _SKIP_COLS:
            return col
    return None


def _set_progress(val: int):
    with _state_lock:
        _app_state["progress"] = val


def _load_shapefile():
    """后台线程：读取 Shapefile 并更新全局状态"""
    global _gdf
    with _state_lock:
        _app_state["loading"] = True
        _app_state["error"]   = None

    try:
        print(f"[LOAD] 开始读取: {SHAPEFILE_PATH}")
        _set_progress(10)

        gdf = gpd.read_file(SHAPEFILE_PATH)
        _set_progress(60)
        print(f"[LOAD] 读取完成，{len(gdf)} 条记录，CRS={gdf.crs}")

        # 统一转为 WGS84
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            print(f"[LOAD] 坐标转换 {gdf.crs} → EPSG:4326 ...")
            gdf = gdf.to_crs("EPSG:4326")
        _set_progress(75)

        # 清理无效几何
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
        gdf = gdf[gdf.geometry.is_valid].copy()
        _set_progress(85)

        # 构建空间索引
        _ = gdf.sindex
        _set_progress(95)

        height_col = _detect_height_col(gdf)
        bounds     = gdf.total_bounds
        num_cols   = [
            c for c in gdf.select_dtypes(include=[np.number]).columns
            if c.lower() not in _SKIP_COLS
        ]

        height_stats = None
        if height_col:
            vals = pd.to_numeric(gdf[height_col], errors="coerce").dropna()
            height_stats = {
                "min":    round(float(vals.min()), 2),
                "max":    round(float(vals.max()), 2),
                "mean":   round(float(vals.mean()), 2),
                "median": round(float(vals.median()), 2),
            }

        with _state_lock:
            _gdf = gdf
            _app_state.update({
                "loaded":          True,
                "loading":         False,
                "progress":        100,
                "total":           len(gdf),
                "columns":         [c for c in gdf.columns if c != "geometry"],
                "numeric_columns": num_cols,
                "height_column":   height_col,
                "height_stats":    height_stats,
                "bounds": {
                    "minx":       float(bounds[0]),
                    "miny":       float(bounds[1]),
                    "maxx":       float(bounds[2]),
                    "maxy":       float(bounds[3]),
                    "center_lon": float((bounds[0] + bounds[2]) / 2),
                    "center_lat": float((bounds[1] + bounds[3]) / 2),
                },
            })

        print(f"[LOAD] 完成！高度列={height_col}，统计={height_stats}")

    except Exception as exc:
        print(f"[ERROR] 加载失败:\n{traceback.format_exc()}")
        with _state_lock:
            _app_state["loading"] = False
            _app_state["error"]   = str(exc)


# ── 公共接口 ───────────────────────────────────────────────────────────────────

def load_shapefile_async():
    """启动后台线程加载 Shapefile"""
    t = threading.Thread(target=_load_shapefile, daemon=True)
    t.start()


def get_state() -> dict:
    with _state_lock:
        return dict(_app_state)


def get_gdf() -> gpd.GeoDataFrame:
    return _gdf
