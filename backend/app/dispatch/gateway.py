"""渠道网关模拟。

真实环境里这里是邮件 / 短信 / 站内信等渠道的适配层。当前实现不依赖外部服务，
按「渠道默认行为 + 单次覆盖（override）」决定每个分段的发送结果，方便验收：

- ok：渠道返回正常回执；
- timeout：渠道超时，调用方应保留部分完成状态，后续只补发缺口；
- error：渠道明确报错（例如渠道停用），错误原因会写进回执；
- empty：渠道没有给出任何回执内容，调用方不得把它计入推送状态。

override 支持测试/验收时精确编排某个渠道下第几次发送的结果，取走即弹出，
避免编排结果残留影响下一轮重试（对应需求里提到的“残留”问题）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.dispatch.models import GATEWAY_EMPTY, GATEWAY_ERROR, GATEWAY_OK, GATEWAY_TIMEOUT

# 渠道默认行为：邮件成功、短信超时，直接对应 INSI-0003 的续发样例。
DEFAULT_CHANNEL_BEHAVIOR: dict[str, str] = {
    "邮件": GATEWAY_OK,
    "短信": GATEWAY_TIMEOUT,
    "站内信": GATEWAY_OK,
    "企业微信": GATEWAY_OK,
}

_FAILURE_REASONS = {
    GATEWAY_TIMEOUT: "渠道响应超时，未收到送达确认",
    GATEWAY_ERROR: "渠道返回错误：渠道暂不可用",
    GATEWAY_EMPTY: "渠道未返回任何回执内容",
}


class ChannelGateway:
    """无外部依赖的渠道网关，线程安全，支持按报告+渠道编排单次结果。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 队列元素为 (结果类型, 可选原因, 发送耗时秒)，按发送顺序消费
        self._overrides: dict[tuple[str, str], list[tuple[str, str | None, float]]] = defaultdict(list)
        # 固定结果：配置后该报告+渠道的每次分段发送都返回同一结果，直到显式取消
        self._fixed: dict[tuple[str, str], tuple[str, str | None, float]] = {}

    def configure(
        self,
        report_code: str,
        channel: str,
        outcomes: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> None:
        """编排某报告在某渠道上接下来若干次「分段发送」的结果，取完即恢复默认行为。"""
        queue = [
            (
                str(item.get("outcome", GATEWAY_OK)),
                item.get("reason"),
                float(item.get("delay", 0.0)),
            )
            for item in outcomes
        ]
        with self._lock:
            self._overrides[(str(report_code), str(channel))] = queue

    def fix_outcome(
        self,
        report_code: str,
        channel: str,
        outcome: str,
        *,
        reason: str | None = None,
        delay: float = 0.0,
    ) -> None:
        """把某渠道固定为同一结果（每次分段发送都生效），适合稳定复现成功/超时渠道。"""
        with self._lock:
            self._fixed[(str(report_code), str(channel))] = (outcome, reason, delay)
            self._overrides.pop((str(report_code), str(channel)), None)

    def reset(self) -> None:
        with self._lock:
            self._overrides.clear()
            self._fixed.clear()

    def _next_outcome(self, report_code: str, channel: str) -> tuple[str, str | None, float]:
        pair = (str(report_code), str(channel))
        with self._lock:
            fixed = self._fixed.get(pair)
            queue = self._overrides.get(pair)
            if queue:
                return queue.pop(0)
        if fixed is not None:
            return fixed
        behavior = DEFAULT_CHANNEL_BEHAVIOR.get(channel, GATEWAY_OK)
        return behavior, None, 0.0

    def send_segment(
        self,
        *,
        report_code: str,
        channel: str,
        recipient_scope: str,
        segment_no: int,
        artifact_version: str,
    ) -> dict[str, Any]:
        """向一个渠道发送一个接收范围下的单个分段，返回渠道原始应答。"""
        outcome, reason, delay = self._next_outcome(report_code, channel)
        if delay:
            time.sleep(delay)
        now = datetime.now().isoformat(timespec="seconds")
        response: dict[str, Any] = {
            "报告编号": report_code,
            "渠道": channel,
            "接收范围": recipient_scope,
            "分段序号": segment_no,
            "产物版本": artifact_version,
            "发送时间": now,
            "outcome": outcome,
        }
        if outcome == GATEWAY_OK:
            # 正常回执：带渠道侧回执号，作为送达凭证
            response["回执号"] = (
                f"RCPT-{report_code}-{channel}-{recipient_scope}"
                f"-{artifact_version}-S{segment_no}-{now.replace(':', '').replace('-', '')}"
            )
            response["状态"] = "送达"
        else:
            response["状态"] = outcome
            response["失败原因"] = reason or _FAILURE_REASONS.get(outcome, "渠道发送失败")
        return response
