#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
建筑查询与禁飞区分类服务
负责：空间裁剪、高度阈值分类、GeoJSON FeatureCollection 生成
"""

import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping


def query_buildings(
    gdf: gpd.GeoDataFrame,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    threshold: float,
    h_col: str | None,
    max_feat: int = 30000,
) -> dict:
    """
    裁剪选区内建筑，按高度阈值分类，返回 GeoJSON FeatureCollection。
    结果包含 stats 字段：total / shown / no_fly / fly / truncated。
    """
    sub   = gdf.cx[minx:maxx, miny:maxy].copy()
    total = len(sub)

    if total == 0:
        return {
            "type": "FeatureCollection",
            "features": [],
            "stats": {"total": 0, "no_fly": 0, "fly": 0, "shown": 0, "truncated": False},
        }

    if h_col and h_col in sub.columns:
        heights = pd.to_numeric(sub[h_col], errors="coerce").fillna(0)
    else:
        heights = pd.Series(0.0, index=sub.index)

    nf_mask      = heights >= threshold
    no_fly_count = int(nf_mask.sum())

    # 超出上限时优先保留禁飞区建筑
    truncated = total > max_feat
    if truncated:
        nf_idx  = nf_mask[nf_mask].index[: max_feat // 2]
        fly_idx = nf_mask[~nf_mask].index[: max_feat // 2]
        keep    = nf_idx.append(fly_idx)
        sub     = sub.loc[keep]
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
                "h":  round(float(heights[idx]), 1),
                "nf": bool(nf_mask[idx]),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "stats": {
            "total":     total,
            "shown":     len(features),
            "no_fly":    no_fly_count,
            "fly":       total - no_fly_count,
            "truncated": truncated,
        },
    }


def prepare_sub(gdf: gpd.GeoDataFrame, minx, miny, maxx, maxy, threshold, h_col):
    """
    裁剪并附加 _h（高度）和 _nf（是否禁飞）字段，供导出器使用。
    """
    sub = gdf.cx[minx:maxx, miny:maxy].copy()
    if h_col and h_col in sub.columns:
        heights = pd.to_numeric(sub[h_col], errors="coerce").fillna(0)
    else:
        heights = pd.Series(0.0, index=sub.index)
    sub["_h"]  = heights
    sub["_nf"] = heights >= threshold
    return sub
