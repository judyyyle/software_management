#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSM → SUMO net.xml 转换器
将 Overpass 下载的 OSM XML 直接解析为 SUMO 路网文件（不依赖 netconvert）。
坐标系：EPSG:32651 (UTM Zone 51N)，以选区左下角为 SUMO 原点。
"""

import math
import xml.etree.ElementTree as _ET
from collections import defaultdict as _dd

from osm_service import HW_SPEED, HW_LANES, HW_PRIO, ONEWAY_HW


def osm_to_sumo_net(osm_xml: str, orig_bounds: tuple, path: str):
    """
    解析 OSM XML，生成 SUMO net.xml。
    orig_bounds: (minx, miny, maxx, maxy) WGS84 经纬度，用于设置 netOffset。
    返回 (节点数, 边数)。
    """
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", "EPSG:32651", always_xy=True)

    root = _ET.fromstring(osm_xml)

    # ── 1. 收集 OSM 节点 ──────────────────────────────────────────────────────
    osm_nodes = {}
    for nd in root.iter("node"):
        nid            = nd.get("id")
        osm_nodes[nid] = (float(nd.get("lon", 0)), float(nd.get("lat", 0)))

    # ── 2. 收集道路 Way ───────────────────────────────────────────────────────
    class _Way:
        __slots__ = ["wid", "nodes", "hw", "oneway", "lanes", "speed", "prio"]

    ways = []
    for way in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.iter("tag")}
        hw   = tags.get("highway", "")
        if hw not in HW_SPEED:
            continue
        w       = _Way()
        w.wid   = way.get("id")
        w.nodes = [r.get("ref") for r in way.iter("nd") if r.get("ref") in osm_nodes]
        if len(w.nodes) < 2:
            continue
        w.hw     = hw
        w.oneway = tags.get("oneway", "no") in ("yes", "1", "true") or hw in ONEWAY_HW
        try:
            w.lanes = max(1, int(tags.get("lanes", HW_LANES.get(hw, 1))))
        except ValueError:
            w.lanes = HW_LANES.get(hw, 1)
        spd     = tags.get("maxspeed", "")
        w.speed = float(spd) / 3.6 if spd.isdigit() else HW_SPEED.get(hw, 11.11)
        w.prio  = HW_PRIO.get(hw, 5)
        ways.append(w)

    if not ways:
        raise ValueError("选区内未找到 OSM 道路数据，请尝试扩大选区范围")

    # ── 3. 确定交叉节点 ───────────────────────────────────────────────────────
    nref = _dd(int)
    for w in ways:
        nref[w.nodes[0]]  += 10
        nref[w.nodes[-1]] += 10
        for n in w.nodes[1:-1]:
            nref[n] += 1
    junctions = {nid for nid, cnt in nref.items() if cnt >= 2}

    # ── 4. UTM 投影 ───────────────────────────────────────────────────────────
    all_nids = {n for w in ways for n in w.nodes}
    utm = {}
    for nid in all_nids:
        if nid in osm_nodes:
            x, y     = tr.transform(*osm_nodes[nid])
            utm[nid] = (float(x), float(y))
    ox, oy = tr.transform(orig_bounds[0], orig_bounds[1])
    ox, oy = float(ox), float(oy)

    # ── 5. 切分 Way → Segment（两交叉节点间） ────────────────────────────────
    segments = []
    for w in ways:
        seg_start = 0
        seg_idx   = 0
        for i in range(1, len(w.nodes)):
            if w.nodes[i] in junctions or i == len(w.nodes) - 1:
                chunk = w.nodes[seg_start: i + 1]
                if len(chunk) >= 2:
                    segments.append((f"e{w.wid}s{seg_idx}", chunk[0], chunk[-1], chunk, w))
                    seg_idx += 1
                seg_start = i

    # ── 6. 组装 net.xml ───────────────────────────────────────────────────────
    used_j    = set()
    for _, fn, tn, _, _ in segments:
        used_j.add(fn)
        used_j.add(tn)

    all_xy    = [utm[n] for n in utm]
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
        f'              convBoundary="{conv_xmin:.2f},{conv_ymin:.2f},{conv_xmax:.2f},{conv_ymax:.2f}"',
        f'              origBoundary="{orig_bounds[0]:.6f},{orig_bounds[1]:.6f},'
        f'{orig_bounds[2]:.6f},{orig_bounds[3]:.6f}"',
        '              projParameter="+proj=utm +zone=51 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>',
    ]

    for nid in used_j:
        if nid not in utm:
            continue
        x, y = utm[nid]
        L.append(f'    <node id="n{nid}" x="{x - ox:.2f}" y="{y - oy:.2f}" type="priority"/>')

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
                L.append(f'    <connection from="{ie}" to="{oe}" fromLane="0" toLane="0"/>')

    L.append("</net>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    n_e = len(edge_from)
    print(f"[OSM→SUMO] {len(used_j)} 节点, {n_e} 条边 → {path}")
    return len(used_j), n_e
