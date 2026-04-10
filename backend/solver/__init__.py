"""
HiveLogix — 求解层

包含调度决策引擎和各类求解算法（贪心、ALNS、DRL等）。

导入规则（依赖 app.py 已将 BASE_DIR 注入 sys.path）：
  from solver.greedy_baseline import GreedyBaseline
  from solver.decision_engine import DispatchDecisionEngine
"""

from solver.decision_engine import DispatchDecisionEngine
from solver.factory import create_solver, list_solvers, register_solver
from solver.greedy_baseline import GreedyBaseline
from solver.interfaces import DispatchSolver
from solver.market_based_solver import MarketBasedSolver

__all__ = [
    "GreedyBaseline",
    "MarketBasedSolver",
    "DispatchDecisionEngine",
    "DispatchSolver",
    "create_solver",
    "register_solver",
    "list_solvers",
]
