#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UAV 禁飞区地图生成器 - 上海建筑高度数据
Flask 后端：加载 shanghai.shp，提供 REST API 供前端查询、可视化、导出。
导出格式：SUMO polygon additional file (.add.xml) / GeoJSON / CSV
"""

import os
import io
import math
import json
import zipfile
import threading
import traceback
import urllib.request as _ureq
import urllib.parse as _uparse
import xml.etree.ElementTree as _ET
from collections import defaultdict as _dd

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping
from flask import Flask, render_template, request, jsonify, send_file, Response

# ─────────────────────────────────────────────────────────────────────────────
# 路径配置
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHAPEFILE_PATH = os.path.join(BASE_DIR, "shanghai_map", "shanghai.shp")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# ─────────────────────────────────────────────────────────────────────────────
# Flask 应用
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 全局状态
# ─────────────────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_app_state = {
    "loaded": False,
    "loading": False,
    "progress": 0,   # 0-100
    "error": None,
    "total": 0,
    "columns": [],
    "numeric_columns": [],
    "height_column": None,
    "height_stats": None,
    "bounds": None,
}
_gdf: gpd.GeoDataFrame = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 高度列自动检测
# ─────────────────────────────────────────────────────────────────────────────
_HEIGHT_PRIORITY = [
    "height", "Height", "HEIGHT",
    "h", "H",
    "bldg_h", "bldg_height", "building_height", "building_h",
    "elev", "elevation", "ELEV",
    "hgt", "HGT",
    "stories", "floors", "floor", "story",
]
_SKIP_COLS = {"fid", "id", "objectid", "osm_id", "gid",
              "area", "perimeter", "shape_area", "shape_len", "shape_leng"}


def _detect_height_col(gdf: gpd.GeoDataFrame):
    """按优先级从数据列中自动选取高度列"""
    num_cols = set(gdf.select_dtypes(include=[np.number]).columns)
    for name in _HEIGHT_PRIORITY:
        if name in gdf.columns and name in num_cols:
            return name
    for col in gdf.columns:
        if col in num_cols and any(kw in col.lower()
                                   for kw in ["height", "elev", "hgt", "floor", "story", "high"]):
            return col
    # 最后备选：第一个不像 ID/面积 的数值列
    for col in num_cols:
        if col.lower() not in _SKIP_COLS:
            return col
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 后台加载 Shapefile
# ─────────────────────────────────────────────────────────────────────────────
def _load_shapefile():
    global _gdf
    with _state_lock:
        _app_state["loading"] = True
        _app_state["error"] = None

    try:
        print(f"[LOAD] 开始读取: {SHAPEFILE_PATH}")
        _set_progress(10)

        gdf = gpd.read_file(SHAPEFILE_PATH)
        _set_progress(60)
        print(f"[LOAD] 读取完成，{len(gdf)} 条记录，CRS={gdf.crs}")
        print(f"[LOAD] 列名: {list(gdf.columns)}")

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
        bounds = gdf.total_bounds
        num_cols = [c for c in gdf.select_dtypes(include=[np.number]).columns
                    if c.lower() not in _SKIP_COLS]

        height_stats = None
        if height_col:
            vals = pd.to_numeric(gdf[height_col], errors="coerce").dropna()
            height_stats = {
                "min": round(float(vals.min()), 2),
                "max": round(float(vals.max()), 2),
                "mean": round(float(vals.mean()), 2),
                "median": round(float(vals.median()), 2),
            }

        with _state_lock:
            _gdf = gdf
            _app_state.update({
                "loaded": True,
                "loading": False,
                "progress": 100,
                "total": len(gdf),
                "columns": [c for c in gdf.columns if c != "geometry"],
                "numeric_columns": num_cols,
                "height_column": height_col,
                "height_stats": height_stats,
                "bounds": {
                    "minx": float(bounds[0]),
                    "miny": float(bounds[1]),
                    "maxx": float(bounds[2]),
                    "maxy": float(bounds[3]),
                    "center_lon": float((bounds[0] + bounds[2]) / 2),
                    "center_lat": float((bounds[1] + bounds[3]) / 2),
                },
            })

        print(f"[LOAD] 完成！高度列={height_col}，统计={height_stats}")

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[ERROR] 加载失败:\n{tb}")
        with _state_lock:
            _app_state["loading"] = False
            _app_state["error"] = str(exc)


def _set_progress(val: int):
    with _state_lock:
        _app_state["progress"] = val


# ─────────────────────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/roads", methods=["POST"])
def api_roads():
    """从 Overpass API 下载选区道路并返回 GeoJSON，供前端黄色叠加层使用。"""
    body  = request.get_json(force=True)
    minx  = float(body["minx"])
    miny  = float(body["miny"])
    maxx  = float(body["maxx"])
    maxy  = float(body["maxy"])
    try:
        osm_xml = _download_osm(minx, miny, maxx, maxy)
        geojson = _osm_to_geojson(osm_xml)
        return jsonify(geojson)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify(dict(_app_state))


@app.route("/api/query", methods=["POST"])
def api_query():
    if not _app_state["loaded"]:
        return jsonify({"error": "数据尚未加载完毕，请稍候"}), 503

    body = request.get_json(force=True)
    minx = float(body["minx"])
    miny = float(body["miny"])
    maxx = float(body["maxx"])
    maxy = float(body["maxy"])
    threshold = float(body.get("threshold", 50))
    h_col = body.get("height_column") or _app_state["height_column"]
    max_feat = min(int(body.get("max", 30000)), 60000)

    try:
        sub = _gdf.cx[minx:maxx, miny:maxy].copy()
        total = len(sub)

        if total == 0:
            return jsonify({
                "type": "FeatureCollection", "features": [],
                "stats": {"total": 0, "no_fly": 0, "fly": 0, "shown": 0, "truncated": False},
            })

        # 高度分类
        if h_col and h_col in sub.columns:
            heights = pd.to_numeric(sub[h_col], errors="coerce").fillna(0)
        else:
            heights = pd.Series(0.0, index=sub.index)

        nf_mask = heights >= threshold
        no_fly_count = int(nf_mask.sum())

        # 若超出限制，优先保留禁飞区建筑
        truncated = total > max_feat
        if truncated:
            nf_idx = nf_mask[nf_mask].index[:max_feat // 2]
            fly_idx = nf_mask[~nf_mask].index[:max_feat // 2]
            keep = nf_idx.append(fly_idx)
            sub = sub.loc[keep]
            heights = heights.loc[keep]
            nf_mask = nf_mask.loc[keep]

        features = []
        for idx, row in sub.iterrows():
            g = row.geometry
            if g is None or g.is_empty:
                continue
            features.append({
                "type": "Feature",
                "geometry": mapping(g),
                "properties": {
                    "h": round(float(heights[idx]), 1),
                    "nf": bool(nf_mask[idx]),
                },
            })

        return jsonify({
            "type": "FeatureCollection",
            "features": features,
            "stats": {
                "total": total,
                "shown": len(features),
                "no_fly": no_fly_count,
                "fly": total - no_fly_count,
                "truncated": truncated,
            },
        })

    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/export", methods=["POST"])
def api_export():
    if not _app_state["loaded"]:
        return jsonify({"error": "数据尚未加载完毕"}), 503

    body = request.get_json(force=True)
    minx = float(body["minx"])
    miny = float(body["miny"])
    maxx = float(body["maxx"])
    maxy = float(body["maxy"])
    threshold = float(body.get("threshold", 50))
    h_col = body.get("height_column") or _app_state["height_column"]
    fmt = body.get("format", "sumo_poly")

    try:
        sub = _gdf.cx[minx:maxx, miny:maxy].copy()
        if h_col and h_col in sub.columns:
            heights = pd.to_numeric(sub[h_col], errors="coerce").fillna(0)
        else:
            heights = pd.Series(0.0, index=sub.index)
        sub["_h"] = heights
        sub["_nf"] = heights >= threshold

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        if fmt == "sumo_poly":
            path = os.path.join(OUTPUT_DIR, "no_fly_zones.add.xml")
            _export_sumo_poly(sub, threshold, path)
            return send_file(path, as_attachment=True,
                             download_name="no_fly_zones.add.xml",
                             mimetype="application/xml")

        if fmt == "geojson":
            path = os.path.join(OUTPUT_DIR, "buildings.geojson")
            out = sub[["geometry", "_h", "_nf"]].copy()
            out.columns = ["geometry", "height_m", "no_fly_zone"]
            out.to_file(path, driver="GeoJSON")
            return send_file(path, as_attachment=True,
                             download_name="buildings.geojson",
                             mimetype="application/geo+json")

        if fmt == "csv":
            path = os.path.join(OUTPUT_DIR, "buildings.csv")
            cen = sub.geometry.centroid
            pd.DataFrame({
                "id": range(len(sub)),
                "height_m": sub["_h"].values,
                "no_fly_zone": sub["_nf"].values,
                "lon": cen.x.values,
                "lat": cen.y.values,
            }).to_csv(path, index=False)
            return send_file(path, as_attachment=True,
                             download_name="buildings.csv",
                             mimetype="text/csv")

        if fmt == "sumo_zip":
            spacing = float(body.get("grid_spacing", 200))
            poly_path = os.path.join(OUTPUT_DIR, "no_fly_zones.add.xml")
            net_path  = os.path.join(OUTPUT_DIR, "grid.net.xml")
            # 先生成 poly 文件，获取 UTM 范围
            gdf_utm = sub.to_crs("EPSG:32651")
            b = gdf_utm.total_bounds
            ox, oy    = float(b[0]), float(b[1])
            width_m   = float(b[2] - b[0])
            height_m2 = float(b[3] - b[1])
            _export_sumo_poly(sub, threshold, poly_path)
            _export_sumo_net(
                ox=ox, oy=oy,
                width_m=width_m, height_m=height_m2,
                grid_spacing=spacing,
                orig_bounds=(minx, miny, maxx, maxy),
                path=net_path,
            )
            # 打包成 zip
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(poly_path, "no_fly_zones.add.xml")
                zf.write(net_path,  "grid.net.xml")
                # 写一个说明文件
                readme = (
                    "UAV 禁飞区 SUMO 完整包\n"
                    "=======================\n"
                    f"高度阈值: {threshold} m\n"
                    f"网格间距: {spacing:.0f} m\n"
                    f"选区范围: ({minx:.6f}, {miny:.6f}) → ({maxx:.6f}, {maxy:.6f})\n"
                    "坐标系:   EPSG:32651 (UTM Zone 51N)\n"
                    "\n"
                    "使用方法:\n"
                    "  sumo-gui -n grid.net.xml --additional-files no_fly_zones.add.xml\n"
                    "\n"
                    "文件说明:\n"
                    "  grid.net.xml          — 覆盖选区的网格路网（自动生成）\n"
                    "  no_fly_zones.add.xml  — 建筑多边形禁飞区叠加层\n"
                    "\n"
                    "在 sumo-gui 中，红色多边形 = 禁飞区，绿色 = 可飞区。\n"
                )
                zf.writestr("README.txt", readme)
            zip_buf.seek(0)
            return send_file(
                zip_buf,
                as_attachment=True,
                download_name="uav_no_fly_sumo.zip",
                mimetype="application/zip",
            )

        if fmt == "sumo_zip_osm":
            poly_path = os.path.join(OUTPUT_DIR, "no_fly_zones.add.xml")
            net_path  = os.path.join(OUTPUT_DIR, "roads.net.xml")
            osm_path  = os.path.join(OUTPUT_DIR, "area.osm")
            _export_sumo_poly(sub, threshold, poly_path)
            print(f"[OSM] 正在下载道路数据 ({minx:.4f},{miny:.4f})→({maxx:.4f},{maxy:.4f}) ...")
            osm_xml = _download_osm(minx, miny, maxx, maxy)
            with open(osm_path, "w", encoding="utf-8") as f:
                f.write(osm_xml)
            n_nodes, n_edges = _osm_to_sumo_net(
                osm_xml=osm_xml,
                orig_bounds=(minx, miny, maxx, maxy),
                path=net_path,
            )
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(poly_path, "no_fly_zones.add.xml")
                zf.write(net_path,  "roads.net.xml")
                zf.write(osm_path,  "area.osm")
                readme = (
                    "UAV 禁飞区 SUMO 真实路网包\n"
                    "===========================\n"
                    f"高度阈值: {threshold} m\n"
                    f"选区范围: ({minx:.6f}, {miny:.6f}) → ({maxx:.6f}, {maxy:.6f})\n"
                    "路网来源: OpenStreetMap (Overpass API)\n"
                    f"节点数:   {n_nodes}\n"
                    f"边数:     {n_edges}\n"
                    "坐标系:   EPSG:32651 (UTM Zone 51N)\n"
                    "\n"
                    "使用方法:\n"
                    "  sumo-gui -n roads.net.xml --additional-files no_fly_zones.add.xml\n"
                    "\n"
                    "文件说明:\n"
                    "  roads.net.xml         — 基于 OSM 真实道路的 SUMO 路网\n"
                    "  no_fly_zones.add.xml  — 建筑多边形禁飞区叠加层\n"
                    "  area.osm              — 原始 OSM 数据（可用 netconvert 重新处理）\n"
                    "\n"
                    "在 sumo-gui 中，红色多边形 = 禁飞区，绿色 = 可飞区。\n"
                )
                zf.writestr("README.txt", readme)
            zip_buf.seek(0)
            return send_file(
                zip_buf,
                as_attachment=True,
                download_name="uav_no_fly_sumo_osm.zip",
                mimetype="application/zip",
            )

        return jsonify({"error": f"未知格式: {fmt}"}), 400

    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# SUMO polygon 导出
# ─────────────────────────────────────────────────────────────────────────────
def _export_sumo_poly(gdf: gpd.GeoDataFrame, threshold: float, path: str):
    """
    将建筑物导出为 SUMO polygon additional file (.add.xml)。
    坐标系：UTM Zone 51N (EPSG:32651)，以选区左下角为原点（方便 SUMO 导入）。
    用法示例：
        sumo-gui -n <your.net.xml> --additional-files no_fly_zones.add.xml
    若无已有路网，可用 netgenerate 生成，例如：
        netgenerate --grid --grid.x-number 20 --grid.y-number 20 \
            --grid.length 200 -o grid.net.xml
        sumo-gui -n grid.net.xml --additional-files no_fly_zones.add.xml
    """
    gdf_utm = gdf.to_crs("EPSG:32651")
    b = gdf_utm.total_bounds
    ox, oy = float(b[0]), float(b[1])
    width_m = float(b[2] - b[0])
    height_m_area = float(b[3] - b[1])

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<!--',
        f'  UAV 禁飞区地图 | 高度阈值: {threshold} m',
        f'  坐标系: EPSG:32651 (UTM Zone 51N)',
        f'  原点(左下): E={ox:.2f} N={oy:.2f}',
        f'  范围: {width_m:.1f} m × {height_m_area:.1f} m',
        f'  红色 (no_fly_zone): 建筑高度 >= {threshold} m',
        f'  绿色 (fly_zone)   : 建筑高度 <  {threshold} m',
        f'-->',
        '<additional xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '            xsi:noNamespaceSchemaLocation='
        '"http://sumo.dlr.de/xsd/additional_file.xsd">',
    ]

    def _write_poly(poly_geom, pid, h, is_nf):
        pts = list(poly_geom.exterior.coords)
        shape = " ".join(f"{x - ox:.2f},{y - oy:.2f}" for x, y in pts)
        color = "255,0,0,160" if is_nf else "0,180,0,80"
        layer = "20" if is_nf else "10"
        ptype = "no_fly_zone" if is_nf else "fly_zone"
        lines.append(
            f'    <poly id="{pid}" type="{ptype}" color="{color}" '
            f'fill="1" layer="{layer}" height="{h:.2f}" shape="{shape}"/>'
        )

    for idx, row in gdf_utm.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        h = float(row.get("_h", 0))
        nf = bool(row.get("_nf", False))
        if geom.geom_type == "Polygon":
            _write_poly(geom, f"b_{idx}", h, nf)
        elif geom.geom_type == "MultiPolygon":
            for i, part in enumerate(geom.geoms):
                _write_poly(part, f"b_{idx}_{i}", h, nf)

    lines.append("</additional>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[EXPORT] 已写入 {len(gdf_utm)} 栋建筑 → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# OSM 道路下载 & 转换为 SUMO net.xml
# ─────────────────────────────────────────────────────────────────────────────
_HW_SPEED = {
    "motorway": 33.33, "trunk": 27.78, "primary": 22.22,
    "secondary": 16.67, "tertiary": 13.89,
    "residential": 8.33, "service": 5.56, "unclassified": 11.11,
    "motorway_link": 22.22, "trunk_link": 16.67,
    "primary_link": 13.89, "secondary_link": 11.11, "tertiary_link": 8.33,
    "living_street": 2.78, "road": 11.11,
}
_HW_LANES = {
    "motorway": 3, "trunk": 2, "primary": 2, "secondary": 2,
    "tertiary": 1, "residential": 1, "service": 1, "unclassified": 1,
    "motorway_link": 1, "trunk_link": 1, "primary_link": 1,
    "secondary_link": 1, "tertiary_link": 1, "living_street": 1, "road": 1,
}
_HW_PRIO = {
    "motorway": 14, "trunk": 13, "primary": 12, "secondary": 11,
    "tertiary": 10, "residential": 5, "service": 3, "unclassified": 4,
    "motorway_link": 9, "trunk_link": 8, "primary_link": 7,
    "secondary_link": 6, "tertiary_link": 5, "living_street": 4, "road": 5,
}
_ONEWAY_HW = {"motorway", "motorway_link"}


def _osm_to_geojson(osm_xml: str) -> dict:
    """将 OSM XML 转换为 GeoJSON FeatureCollection（线段），用于前端地图道路叠加层。"""
    root = _ET.fromstring(osm_xml)
    nodes = {}
    for nd in root.iter("node"):
        nodes[nd.get("id")] = (float(nd.get("lon", 0)), float(nd.get("lat", 0)))

    features = []
    for way in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.iter("tag")}
        hw = tags.get("highway", "")
        if hw not in _HW_SPEED:
            continue
        coords = [nodes[r.get("ref")] for r in way.iter("nd") if r.get("ref") in nodes]
        if len(coords) < 2:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"highway": hw, "name": tags.get("name", "")},
        })
    return {"type": "FeatureCollection", "features": features}


def _download_osm(minx: float, miny: float, maxx: float, maxy: float) -> str:
    """通过 Overpass API 下载选区内 OSM 道路数据，返回 XML 文本。"""
    query = (
        f"[out:xml][timeout:90];\n"
        f"(\n"
        f'  way["highway"]({miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f});\n'
        f"  node(w);\n"
        f");\n"
        f"out body;\n"
        f">;\n"
        f"out skel qt;\n"
    )
    data = _uparse.urlencode({"data": query}).encode("utf-8")
    req  = _ureq.Request(
        "https://overpass-api.de/api/interpreter",
        data=data,
        headers={"User-Agent": "UAV-NoFlyMap/1.0"},
    )
    with _ureq.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8")


def _osm_to_sumo_net(osm_xml: str, orig_bounds: tuple, path: str):
    """
    将 OSM XML 解析并转换为 SUMO net.xml（不依赖 netconvert）。
    坐标系: EPSG:32651 (UTM Zone 51N)，以选区左下角为 SUMO 原点。
    返回 (节点数, 边数)。
    """
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)

    root = _ET.fromstring(osm_xml)

    # ── 1. 收集 OSM 节点 ──
    osm_nodes = {}
    for nd in root.iter("node"):
        nid = nd.get("id")
        osm_nodes[nid] = (float(nd.get("lon", 0)), float(nd.get("lat", 0)))

    # ── 2. 收集 Way ──
    class _W:
        __slots__ = ["wid", "nodes", "hw", "oneway", "lanes", "speed", "prio"]

    ways = []
    for way in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.iter("tag")}
        hw = tags.get("highway", "")
        if hw not in _HW_SPEED:
            continue
        w = _W()
        w.wid   = way.get("id")
        w.nodes = [r.get("ref") for r in way.iter("nd")
                   if r.get("ref") in osm_nodes]
        if len(w.nodes) < 2:
            continue
        w.hw     = hw
        w.oneway = tags.get("oneway", "no") in ("yes", "1", "true") or hw in _ONEWAY_HW
        try:
            w.lanes = max(1, int(tags.get("lanes", _HW_LANES.get(hw, 1))))
        except ValueError:
            w.lanes = _HW_LANES.get(hw, 1)
        spd = tags.get("maxspeed", "")
        w.speed = float(spd) / 3.6 if spd.isdigit() else _HW_SPEED.get(hw, 11.11)
        w.prio  = _HW_PRIO.get(hw, 5)
        ways.append(w)

    if not ways:
        raise ValueError("选区内未找到 OSM 道路数据，请尝试扩大选区范围")

    # ── 3. 确定交叉节点 ──
    nref = _dd(int)
    for w in ways:
        nref[w.nodes[0]]  += 10   # 端点强制为交叉节点
        nref[w.nodes[-1]] += 10
        for n in w.nodes[1:-1]:
            nref[n] += 1
    junctions = {nid for nid, cnt in nref.items() if cnt >= 2}

    # ── 4. UTM 投影 ──
    all_nids = {n for w in ways for n in w.nodes}
    utm = {}
    for nid in all_nids:
        if nid in osm_nodes:
            x, y = tr.transform(*osm_nodes[nid])
            utm[nid] = (float(x), float(y))
    ox, oy = tr.transform(orig_bounds[0], orig_bounds[1])
    ox, oy = float(ox), float(oy)

    # ── 5. 切分 Way → Segment（两交叉节点之间）──
    # segment: (sid, from_nid, to_nid, [node_ids], _W)
    segments = []
    for w in ways:
        seg_start = 0
        seg_idx   = 0
        for i in range(1, len(w.nodes)):
            if w.nodes[i] in junctions or i == len(w.nodes) - 1:
                chunk = w.nodes[seg_start: i + 1]
                if len(chunk) >= 2:
                    segments.append((
                        f"e{w.wid}s{seg_idx}",
                        chunk[0], chunk[-1], chunk, w,
                    ))
                    seg_idx += 1
                seg_start = i

    # ── 6. 组装 net.xml ──
    used_j = set()
    for _, fn, tn, _, _ in segments:
        used_j.add(fn)
        used_j.add(tn)

    all_xy = [utm[n] for n in utm]
    conv_xmin = min(x - ox for x, y in all_xy)
    conv_ymin = min(y - oy for x, y in all_xy)
    conv_xmax = max(x - ox for x, y in all_xy)
    conv_ymax = max(y - oy for x, y in all_xy)

    L = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<net version="1.16" junctionCornerDetail="5" limitTurnSpeed="5.50"',
        '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '     xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/net_file.xsd">',
        f'    <location netOffset="{-ox:.2f},{-oy:.2f}"',
        f'              convBoundary="{conv_xmin:.2f},{conv_ymin:.2f},'
        f'{conv_xmax:.2f},{conv_ymax:.2f}"',
        f'              origBoundary="{orig_bounds[0]:.6f},{orig_bounds[1]:.6f},'
        f'{orig_bounds[2]:.6f},{orig_bounds[3]:.6f}"',
        '              projParameter="+proj=utm +zone=51 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>',
    ]

    # 节点
    for nid in used_j:
        if nid not in utm:
            continue
        x, y = utm[nid]
        L.append(f'    <node id="n{nid}" x="{x - ox:.2f}" y="{y - oy:.2f}" type="priority"/>')

    # 边
    edge_from = {}
    edge_to   = {}

    def _add_edge(eid, fn, tn, coords, w):
        if len(coords) < 2:
            return
        length = sum(
            math.hypot(coords[k+1][0] - coords[k][0], coords[k+1][1] - coords[k][1])
            for k in range(len(coords) - 1)
        )
        if length < 0.1:
            return
        shape = " ".join(f"{x - ox:.2f},{y - oy:.2f}" for x, y in coords)
        L.append(
            f'    <edge id="{eid}" from="n{fn}" to="n{tn}"'
            f' priority="{w.prio}" numLanes="{w.lanes}" speed="{w.speed:.2f}">'
        )
        for li in range(w.lanes):
            L.append(
                f'        <lane id="{eid}_{li}" index="{li}"'
                f' speed="{w.speed:.2f}" length="{length:.2f}" width="3.20"'
                f' shape="{shape}"/>'
            )
        L.append("    </edge>")
        edge_from[eid] = fn
        edge_to[eid]   = tn

    for sid, fn, tn, node_list, w in segments:
        fwd = [utm[n] for n in node_list if n in utm]
        _add_edge(sid, fn, tn, fwd, w)
        if not w.oneway:
            _add_edge(f"r{sid}", tn, fn, list(reversed(fwd)), w)

    # 连接（各交叉口：所有进入边→所有离开边，跳过掉头）
    incoming = _dd(list)
    outgoing = _dd(list)
    for eid in edge_from:
        outgoing[edge_from[eid]].append(eid)
        incoming[edge_to[eid]].append(eid)

    for node in used_j:
        for ie in incoming.get(node, []):
            for oe in outgoing.get(node, []):
                if edge_from[ie] == edge_to[oe]:   # 跳过掉头
                    continue
                L.append(
                    f'    <connection from="{ie}" to="{oe}"'
                    f' fromLane="0" toLane="0"/>'
                )

    L.append("</net>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    n_e = len(edge_from)
    print(f"[OSM→SUMO] {len(used_j)} 节点, {n_e} 条边 → {path}")
    return len(used_j), n_e


# ─────────────────────────────────────────────────────────────────────────────
# SUMO grid 路网生成（备用，选区内无 OSM 数据时使用）
# ─────────────────────────────────────────────────────────────────────────────
def _export_sumo_net(ox: float, oy: float,
                     width_m: float, height_m: float,
                     grid_spacing: float,
                     orig_bounds: tuple,
                     path: str):
    """
    纯 Python 生成覆盖选区的 SUMO grid net.xml，无需安装 SUMO。
    坐标系与 no_fly_zones.add.xml 一致（EPSG:32651，以选区左下角为原点）。
    """
    nx = int(math.ceil(width_m  / grid_spacing)) + 1
    ny = int(math.ceil(height_m / grid_spacing)) + 1
    xs = [min(i * grid_spacing, width_m)  for i in range(nx)]
    ys = [min(j * grid_spacing, height_m) for j in range(ny)]
    speed = 13.89  # 50 km/h

    L = []
    L += [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<net version="1.16" junctionCornerDetail="5" limitTurnSpeed="5.50"',
        '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '     xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/net_file.xsd">',
        f'    <location netOffset="{-ox:.2f},{-oy:.2f}"',
        f'              convBoundary="0.00,0.00,{width_m:.2f},{height_m:.2f}"',
        f'              origBoundary="{orig_bounds[0]:.6f},{orig_bounds[1]:.6f},'
        f'{orig_bounds[2]:.6f},{orig_bounds[3]:.6f}"',
        '              projParameter="+proj=utm +zone=51 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>',
    ]

    # ── 节点 ──
    for j in range(ny):
        for i in range(nx):
            L.append(f'    <node id="n{i}_{j}" x="{xs[i]:.2f}" y="{ys[j]:.2f}" type="priority"/>')

    # ── 边（双向） ──
    edges = {}   # eid -> (from_node, to_node)

    # 水平方向
    for j in range(ny):
        for i in range(nx - 1):
            ln = xs[i + 1] - xs[i]
            for eid, fn, tn, sx0, sy0, sx1, sy1 in [
                (f"h{i}_{j}_f", f"n{i}_{j}",   f"n{i+1}_{j}",
                 xs[i], ys[j], xs[i+1], ys[j]),
                (f"h{i}_{j}_b", f"n{i+1}_{j}", f"n{i}_{j}",
                 xs[i+1], ys[j], xs[i], ys[j]),
            ]:
                edges[eid] = (fn, tn)
                L += [
                    f'    <edge id="{eid}" from="{fn}" to="{tn}" priority="2" numLanes="1" speed="{speed}">',
                    f'        <lane id="{eid}_0" index="0" speed="{speed}" length="{ln:.2f}" width="3.20"'
                    f' shape="{sx0:.2f},{sy0:.2f} {sx1:.2f},{sy1:.2f}"/>',
                    '    </edge>',
                ]

    # 垂直方向
    for j in range(ny - 1):
        for i in range(nx):
            ln = ys[j + 1] - ys[j]
            for eid, fn, tn, sx0, sy0, sx1, sy1 in [
                (f"v{i}_{j}_f", f"n{i}_{j}",   f"n{i}_{j+1}",
                 xs[i], ys[j], xs[i], ys[j+1]),
                (f"v{i}_{j}_b", f"n{i}_{j+1}", f"n{i}_{j}",
                 xs[i], ys[j+1], xs[i], ys[j]),
            ]:
                edges[eid] = (fn, tn)
                L += [
                    f'    <edge id="{eid}" from="{fn}" to="{tn}" priority="2" numLanes="1" speed="{speed}">',
                    f'        <lane id="{eid}_0" index="0" speed="{speed}" length="{ln:.2f}" width="3.20"'
                    f' shape="{sx0:.2f},{sy0:.2f} {sx1:.2f},{sy1:.2f}"/>',
                    '    </edge>',
                ]

    # ── 连接（交叉口通行关系，跳过 U 形掉头） ──
    incoming = {}   # node -> [eid]
    outgoing = {}   # node -> [eid]
    for eid, (fn, tn) in edges.items():
        outgoing.setdefault(fn, []).append(eid)
        incoming.setdefault(tn, []).append(eid)

    for node, in_edges in incoming.items():
        for ie in in_edges:
            ie_from = edges[ie][0]
            for oe in outgoing.get(node, []):
                oe_to = edges[oe][1]
                if ie_from == oe_to:   # 跳过掉头
                    continue
                L.append(f'    <connection from="{ie}" to="{oe}" fromLane="0" toLane="0"/>')

    L.append('</net>')

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print(f"[EXPORT] grid.net.xml 已写入: {nx}×{ny} 节点, {len(edges)} 条边 → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=_load_shapefile, daemon=True)
    t.start()
    print("[INFO] 服务器启动: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
