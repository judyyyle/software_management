#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动 Phase 4 的 SUMO GUI 验证。

默认加载：
  backend/test_data/default_scene/sumo/phase4_truck_route/truck_route.sumocfg

用法：
  python backend/scripts/run_phase4_sumo_gui.py
  python backend/scripts/run_phase4_sumo_gui.py --regenerate
  python backend/scripts/run_phase4_sumo_gui.py --sumo-gui-bin /path/to/sumo-gui
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from training.export_sumo_truck_route import export_phase4_truck_route


DEFAULT_SUMOCFG = (
    REPO_ROOT
    / "backend"
    / "test_data"
    / "default_scene"
    / "sumo"
    / "phase4_truck_route"
    / "truck_route.sumocfg"
)


def _resolve_sumo_gui(binary_override: str | None) -> str:
    if binary_override:
        return binary_override
    found = shutil.which("sumo-gui")
    if found:
        return found
    raise FileNotFoundError(
        "未找到 `sumo-gui`。请先安装 SUMO，或使用 "
        "`--sumo-gui-bin /path/to/sumo-gui` 指定可执行文件。"
    )


def _resolve_netconvert(sumo_gui_bin: str) -> str | None:
    sibling = Path(sumo_gui_bin).resolve().with_name("netconvert")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("netconvert")


def _sumocfg_net_path(sumocfg: Path) -> Path | None:
    root = ET.parse(sumocfg).getroot()
    for elem in root.iter():
        if elem.tag.endswith("net-file"):
            value = elem.get("value")
            if value:
                return (sumocfg.parent / value).resolve()
    return None


def _normalize_net_with_netconvert(*, sumocfg: Path, netconvert_bin: str | None) -> None:
    if not netconvert_bin:
        return
    net_path = _sumocfg_net_path(sumocfg)
    if not net_path or not net_path.is_file():
        return
    tmp_path = net_path.with_suffix(".netconvert.tmp.xml")
    result = subprocess.run(
        [
            netconvert_bin,
            "-s",
            str(net_path),
            "-o",
            str(tmp_path),
            "--ignore-errors",
            "--ignore-errors.connections",
        ],
        capture_output=True,
        text=True,
        cwd=str(sumocfg.parent),
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    tmp_path.replace(net_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 Phase 4 SUMO GUI 验证")
    parser.add_argument(
        "--sumocfg",
        type=str,
        default=str(DEFAULT_SUMOCFG),
        help="要加载的 .sumocfg 路径",
    )
    parser.add_argument(
        "--sumo-gui-bin",
        type=str,
        default="",
        help="sumo-gui 可执行文件路径；为空时自动从 PATH 查找",
    )
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="启动前先重新生成 Phase 4 导出产物",
    )
    args = parser.parse_args(argv)

    if args.regenerate:
        export_phase4_truck_route()

    sumocfg = Path(args.sumocfg).resolve()
    if not sumocfg.is_file():
        raise FileNotFoundError(
            f"SUMO 配置文件不存在: {sumocfg}\n"
            "请先运行 `python backend/training/export_sumo_truck_route.py` "
            "或加上 `--regenerate`。"
        )

    sumo_gui_bin = _resolve_sumo_gui(args.sumo_gui_bin or None)
    _normalize_net_with_netconvert(
        sumocfg=sumocfg,
        netconvert_bin=_resolve_netconvert(sumo_gui_bin),
    )
    env = os.environ.copy()
    env.setdefault("QT_X11_NO_MITSHM", "1")
    env.setdefault("LIBGL_ALWAYS_INDIRECT", "1")
    subprocess.run(
        [sumo_gui_bin, "-c", str(sumocfg), "--disable-textures"],
        check=True,
        cwd=str(sumocfg.parent),
        env=env,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
