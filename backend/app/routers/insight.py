"""洞察推送接口：报告登记、状态流转，以及多渠道推送的去重/续发/回执链路。

调度相关接口都挂在 /api/insight 下，保证“以洞察报表为业务入口”：
- POST /{id}/artifact       生成或换版报表产物（换版自动失效旧回执）
- POST /{id}/push           按渠道+接收范围推送（成功复用、失败留缺口）
- POST /{id}/retry          失败续发，只补缺口渠道
- GET  /{id}/dispatch       推送全景：版本一致性、渠道回执、待补渠道、四类计数
- GET  /{id}/receipts       回执明细分页（同一条回执绝不重复显示）
- GET  /{id}/runs           调度执行记录
- POST /dispatch/reset      清空调度域数据（验收/测试用）
- POST /dispatch/demo       构造 INSI-0003 邮件成功、短信超时的续发样例
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, ArtifactPayload, EntryPayload, PageResult, PushPayload
from app.dispatch import dispatch_engine
from app.services.insight import InsightService

router = APIRouter(prefix="/api/insight", tags=["洞察推送"])

service = InsightService()

LIST_FIELDS = ["报告编号", "报告标题", "分析主题", "关键发现", "数据来源", "生成时间", "推送渠道", "推送状态"]
STATUSES = ["草稿", "生成中", "部分完成", "已推送", "推送失败", "已撤回"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    status: str | None = Query(default=None, description="草稿、生成中、部分完成、已推送、推送失败、已撤回"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报告编号与状态过滤洞察推送列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/dispatch/reset", include_in_schema=False)
def reset_dispatch_get() -> dict[str, Any]:
    """兼容浏览器直接访问的重置入口。"""
    dispatch_engine.reset()
    return {"ok": True, "message": "洞察调度数据已清空，可重新演练推送与续发"}


@router.post("/dispatch/reset")
def reset_dispatch() -> dict[str, Any]:
    """清空调度域（产物、回执、执行记录、网关编排），用于验收前回到干净起点。"""
    dispatch_engine.reset()
    return {"ok": True, "message": "洞察调度数据已清空，可重新演练推送与续发"}


@router.post("/dispatch/demo")
def seed_demo() -> dict[str, Any]:
    """构造续发样例：INSI-0003 邮件渠道成功、短信渠道超时，报告停在部分完成。"""
    try:
        return dispatch_engine.seed_demo()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条洞察报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条洞察报告，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="洞察报告已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交生成 / 推送报告 / 撤回报告；推送动作经调度引擎处理去重与续发。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message, dispatch_result = service.run_action(entry_id, action)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/artifact")
def generate_artifact(entry_id: int, payload: ArtifactPayload | None = None) -> dict[str, Any]:
    """生成报表产物；再次调用即换版，旧版本回执自动失效仅作历史。"""
    try:
        artifact, counter = dispatch_engine.generate_artifact(
            entry_id,
            channels=payload.channels if payload else None,
            scopes=payload.scopes if payload else None,
            segment_count=payload.segment_count if payload else 3,
            change_report_version=payload.change_report_version if payload else False,
            note=payload.note or "" if payload else "",
            scheduler=payload.scheduler if payload else "manual",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "产物": artifact, "计数": counter.as_dict()}


@router.post("/{entry_id}/push")
def push_report(entry_id: int, payload: PushPayload | None = None) -> dict[str, Any]:
    """多渠道推送：成功渠道命中复用，失败渠道保留部分完成并列出待补渠道。"""
    try:
        return dispatch_engine.push(
            entry_id,
            channels=payload.channels if payload else None,
            scopes=payload.scopes if payload else None,
            segment_count=payload.segment_count if payload else None,
            scheduler=payload.scheduler if payload else "manual",
            overrides=payload.overrides if payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{entry_id}/retry")
def retry_report(entry_id: int, payload: PushPayload | None = None) -> dict[str, Any]:
    """失败续发：只补发当前产物下仍有缺口的渠道，已成功渠道不重发。"""
    overrides = payload.overrides if payload else None
    if overrides:
        report = dispatch_engine.get_report(entry_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在或已归档")
        for channel, outcomes in overrides.items():
            dispatch_engine.gateway.configure(str(report["报告编号"]), channel, outcomes)
    try:
        return dispatch_engine.retry_gaps(entry_id, scheduler=(payload.scheduler if payload else "manual"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{entry_id}/dispatch")
def dispatch_status(entry_id: int) -> dict[str, Any]:
    """推送全景：报告与当前产物的版本一致性、渠道回执、待补渠道与命中/补发/冲突/失效计数。"""
    try:
        return dispatch_engine.get_status(entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{entry_id}/receipts")
def list_receipts(
    entry_id: int,
    include_history: bool = Query(default=False, description="是否包含换版后失效的历史回执"),
    channel: str | None = Query(default=None, description="按渠道过滤"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """回执明细分页；每个四元组仅一条，分段明细内嵌，翻页不会重复显示同一条。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    try:
        return dispatch_engine.list_receipts(
            entry_id, include_history=include_history, channel=channel, page=page, size=size
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{entry_id}/runs")
def list_runs(entry_id: int) -> dict[str, Any]:
    """调度执行记录：每次推送/续发/换版的命中、补发、冲突、失效与渠道级结果。"""
    try:
        runs = dispatch_engine.list_runs(entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": runs, "total": len(runs)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出洞察推送清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "insight", "total": total, "items": items}
