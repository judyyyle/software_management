#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSM 数据获取与道路 GeoJSON 转换服务
负责：通过 Overpass API 下载道路数据、转换为 GeoJSON 前端叠加层
道路类型常量（HW_*）同时供 exporters/sumo_net_osm.py 使用
"""

import urllib.request as _ureq
import urllib.parse  as _uparse
import xml.etree.ElementTree as _ET

# ── 道路类型参数表 ─────────────────────────────────────────────────────────────
HW_SPEED = {
    "motorway": 33.33,       "trunk": 27.78,         "primary": 22.22,
    "secondary": 16.67,      "tertiary": 13.89,
    "residential": 8.33,     "service": 5.56,         "unclassified": 11.11,
    "motorway_link": 22.22,  "trunk_link": 16.67,
    "primary_link": 13.89,   "secondary_link": 11.11, "tertiary_link": 8.33,
    "living_street": 2.78,   "road": 11.11,
}
HW_LANES = {
    "motorway": 3,  "trunk": 2,     "primary": 2,     "secondary": 2,
    "tertiary": 1,  "residential": 1, "service": 1,   "unclassified": 1,
    "motorway_link": 1,  "trunk_link": 1,  "primary_link": 1,
    "secondary_link": 1, "tertiary_link": 1, "living_street": 1, "road": 1,
}
HW_PRIO = {
    "motorway": 14, "trunk": 13,    "primary": 12,    "secondary": 11,
    "tertiary": 10, "residential": 5, "service": 3,   "unclassified": 4,
    "motorway_link": 9,  "trunk_link": 8,  "primary_link": 7,
    "secondary_link": 6, "tertiary_link": 5, "living_street": 4, "road": 5,
}
ONEWAY_HW = {"motorway", "motorway_link"}


def download_osm(minx: float, miny: float, maxx: float, maxy: float) -> str:
    """通过 Overpass API 下载选区内道路 OSM 数据，返回 XML 字符串。"""
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


def osm_to_geojson(osm_xml: str) -> dict:
    """将 OSM XML 转换为 GeoJSON FeatureCollection（道路线段），供前端叠加层使用。"""
    root   = _ET.fromstring(osm_xml)
    nodes  = {}
    for nd in root.iter("node"):
        nodes[nd.get("id")] = (float(nd.get("lon", 0)), float(nd.get("lat", 0)))

    features = []
    for way in root.iter("way"):
        tags   = {t.get("k"): t.get("v") for t in way.iter("tag")}
        hw     = tags.get("highway", "")
        if hw not in HW_SPEED:
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
