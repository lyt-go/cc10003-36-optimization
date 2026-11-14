"""洞察调度域的状态口径与去重键定义。

去重与重试边界（需求约定）：
- 一条渠道回执由「报表产物版本 + 报告版本 + 渠道 + 接收范围」四元组唯一识别；
- 同一四元组在任意并发场景下只允许有一条生效回执（另一条记为冲突，不产生重复回执）；
- 产物换版后，旧四元组回执整体转为历史（失效），不再参与新版本的推送状态统计；
- 空回执（渠道没有任何回执内容）不写成回执，更不能计入成功状态。
"""
from __future__ import annotations

from typing import Any

# 回执状态：只有 SUCCESS 可以复用，其余状态在重试时只从缺口续发。
RECEIPT_SUCCESS = "成功"
RECEIPT_TIMEOUT = "超时"
RECEIPT_FAILED = "失败"
RECEIPT_EMPTY = "空回执"
# 回执处于重试中间态：已经发送成功一部分分段，剩余分段待补。
RECEIPT_PARTIAL = "部分完成"
# 旧产物版本留下的回执：仅作历史可查，不参与当前版本推送状态。
RECEIPT_SUPERSEDED = "已失效"

# 报告整体推送状态
REPORT_PUSH_FULL = "全部完成"
REPORT_PUSH_PARTIAL = "部分完成"
REPORT_PUSH_FAILED = "全部失败"
REPORT_PUSH_NONE = "未推送"

# 渠道网关模拟结果
GATEWAY_OK = "ok"
GATEWAY_TIMEOUT = "timeout"
GATEWAY_ERROR = "error"
GATEWAY_EMPTY = "empty"

# 渠道分段推送时，单段的两种结果
SEGMENT_DELIVERED = "已送达"
SEGMENT_GAP = "待补发"


def dedup_key(
    artifact_version: str,
    report_version: str,
    channel: str,
    recipient_scope: str,
) -> tuple[str, str, str, str]:
    """四元组去重键。所有字段统一按字符串归一化，避免 None / 数字造成重复键。"""
    return (
        str(artifact_version).strip(),
        str(report_version).strip(),
        str(channel).strip(),
        str(recipient_scope).strip(),
    )


def receipt_key_of(receipt: dict[str, Any]) -> tuple[str, str, str, str]:
    """从回执记录反推四元组键。"""
    return dedup_key(
        receipt["产物版本"],
        receipt["报告版本"],
        receipt["渠道"],
        receipt["接收范围"],
    )


class Counter:
    """一次调度动作的命中/补发/冲突/失效计数（可变，跨渠道累计同一个实例）。

    命中：直接复用已成功渠道的回执，未重复发送；
    补发：失败渠道只对缺口分段执行的续发；
    冲突：并发调度争抢同一四元组时被拦截的次数（不产生重复回执）；
    失效：因产物换版被转为历史的旧回执数量。
    """

    __slots__ = ("hit", "retry", "conflict", "stale")

    def __init__(self, hit: int = 0, retry: int = 0, conflict: int = 0, stale: int = 0) -> None:
        self.hit = hit
        self.retry = retry
        self.conflict = conflict
        self.stale = stale

    def add_hit(self) -> None:
        self.hit += 1

    def add_retry(self) -> None:
        self.retry += 1

    def add_conflict(self) -> None:
        self.conflict += 1

    def add_stale(self) -> None:
        self.stale += 1

    def add(self, other: "Counter") -> "Counter":
        return Counter(
            hit=self.hit + other.hit,
            retry=self.retry + other.retry,
            conflict=self.conflict + other.conflict,
            stale=self.stale + other.stale,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Counter":
        return cls(
            hit=int(raw.get("命中", 0)),
            retry=int(raw.get("补发", 0)),
            conflict=int(raw.get("冲突", 0)),
            stale=int(raw.get("失效", 0)),
        )

    def as_dict(self) -> dict[str, int]:
        return {"命中": self.hit, "补发": self.retry, "冲突": self.conflict, "失效": self.stale}
