"""洞察调度推送域：报表产物版本、渠道回执去重与失败续发。

模块划分：
- models：状态/计数常量与四元组去重键的口径
- gateway：渠道网关模拟（成功 / 超时 / 错误 / 空回执）
- engine：线程安全的调度引擎，串起产物、报告、回执与执行记录
"""
from __future__ import annotations

from app.dispatch.engine import DispatchEngine, dispatch_engine

__all__ = ["DispatchEngine", "dispatch_engine"]
