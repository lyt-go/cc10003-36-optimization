"""洞察推送业务规则：报告生成、多渠道推送、回执去重与失败续发。

普通的列表/登记逻辑保留在本服务里，真正的调度推送链路（产物版本、渠道回执、
并发去重、缺口续发、换版失效）统一收敛在 app.dispatch 引擎，避免两边各算一套状态。
"""
from __future__ import annotations

from typing import Any

from app.dispatch import dispatch_engine
from app.store import store

MODULE = "insight"
REQUIRED_FIELDS = ["报告编号", "报告标题", "分析主题"]
STATUS_ORDER = ["草稿", "生成中", "部分完成", "已推送", "推送失败", "已撤回"]
ACTION_RULES = {"提交生成": "生成中", "推送报告": "已推送", "撤回报告": "已撤回"}
NEGATIVE_ACTIONS = []


class InsightService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """执行报告动作。

        返回 (报告, 提示信息, 调度结果)。推送相关动作走调度引擎，保证产物、报告、
        渠道回执共享生成版本，并复用成功渠道、只续发失败缺口。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"洞察报告 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于洞察推送可执行范围", None

        dispatch_result: dict[str, Any] | None = None
        if action == "提交生成":
            artifact, _ = dispatch_engine.generate_artifact(entry_id)
            entry["status"] = "生成中"
            entry["pending"] = True
            entry["abnormal"] = False
            message = f"报表产物 {artifact['产物编号']}（{artifact['产物版本']}）已生成，待推送"
        elif action == "推送报告":
            dispatch_result = dispatch_engine.push(entry_id)
            entry = dispatch_engine.get_report(entry_id) or entry
            push_status = dispatch_result["报告推送状态"]["推送状态"]
            message = f"推送完成：{push_status}，计数 {dispatch_result['计数']}"
        else:  # 撤回报告
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里", None
            entry["status"] = target
            entry["pending"] = False
            entry["abnormal"] = False
            message = "洞察报告已撤回"
        return entry, message, dispatch_result
