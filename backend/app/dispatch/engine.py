"""洞察调度引擎：报表产物、洞察报告与渠道回执共享生成版本的推送链路。

设计要点（对应需求的去重与重试边界）：

1. 四元组去重
   回执唯一键 = （产物版本, 报告版本, 渠道, 接收范围）。一个键在生效期内只有一条回执，
   分段发送结果挂在同一条回执的「分段明细」里，分段查看天然不会重复。

2. 成功复用、失败续发
   命中成功回执的渠道直接复用（命中计数 +1），不再调用网关；
   未成功的渠道只对缺口分段续发（补发按段计数），已送达分段绝不重发。

3. 并发互斥
   每个四元组一把锁 + 进行中标记。多个调度任务并发处理同一报告时，
   后来者命中进行中标记即记为冲突并跳过，不产生第二条回执。

4. 换版失效
   产物换版时，旧产物版本下的全部回执置为「已失效」仅作历史，
   新版本的推送状态只统计新版本回执，杜绝旧渠道结果被误用。

5. 空回执不落地
   渠道返回空内容时不写回执、不计成功，只在执行记录与失败原因里留痕。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.dispatch.gateway import ChannelGateway
from app.dispatch.models import (
    GATEWAY_EMPTY,
    GATEWAY_ERROR,
    GATEWAY_OK,
    GATEWAY_TIMEOUT,
    RECEIPT_EMPTY,
    RECEIPT_FAILED,
    RECEIPT_PARTIAL,
    RECEIPT_SUCCESS,
    RECEIPT_SUPERSEDED,
    RECEIPT_TIMEOUT,
    REPORT_PUSH_FAILED,
    REPORT_PUSH_FULL,
    REPORT_PUSH_NONE,
    REPORT_PUSH_PARTIAL,
    SEGMENT_DELIVERED,
    SEGMENT_GAP,
    Counter,
    dedup_key,
    receipt_key_of,
)
from app.store import store

INSIGHT_MODULE = "insight"

DEFAULT_CHANNELS = ["邮件", "短信"]
DEFAULT_SCOPES = ["默认接收范围"]
DEFAULT_SEGMENTS = 3


class DispatchEngine:
    """内存版调度引擎；真实项目里这些表与锁会换成数据库唯一索引与事务。"""

    def __init__(self) -> None:
        self._meta_lock = threading.RLock()
        self._key_locks: dict[tuple[str, str, str, str], threading.Lock] = {}
        self._in_progress: set[tuple[str, str, str, str]] = set()
        self.gateway = ChannelGateway()
        self.artifacts: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        # 每次推送登记过的期望渠道组合（按产物版本），即使空回执被丢弃，
        # 待补渠道仍能列出该组合，不会从报告状态里“消失”。
        self._expected_keys: dict[str, set[tuple[str, str]]] = {}
        self._booted = False

    # ------------------------------------------------------------------
    # 初始化与演示数据
    # ------------------------------------------------------------------
    def bootstrap(self) -> None:
        """给既有洞察报告补齐调度字段；幂等，重复调用不会重复造数据。"""
        with self._meta_lock:
            if self._booted:
                return
            for row in store.rows(INSIGHT_MODULE):
                row.setdefault("报告版本", None)
                row.setdefault("生成版本", None)
                row.setdefault("推送状态", REPORT_PUSH_NONE)
                row.setdefault("待补渠道", [])
                row.setdefault("失败原因", {})
                row.setdefault("渠道配置", [])
            self._booted = True

    def reset(self) -> None:
        """清空调度域数据并重新挂载洞察报告，供测试与验收反复演练。"""
        with self._meta_lock:
            self.artifacts.clear()
            self.receipts.clear()
            self.runs.clear()
            self._in_progress.clear()
            self._key_locks.clear()
            self._expected_keys.clear()
            self.gateway.reset()
            for row in store.rows(INSIGHT_MODULE):
                row["报告版本"] = None
                row["生成版本"] = None
                row["推送状态"] = REPORT_PUSH_NONE
                row["待补渠道"] = []
                row["失败原因"] = {}
                row["渠道配置"] = []
                # 产物与回执都已清空，调度过程态回到草稿；已撤回是业务动作结果，保留
                if row["status"] in ("生成中", "部分完成", "已推送", "推送失败"):
                    row["status"] = "草稿"
                row["pending"] = row["status"] != "已撤回"
                row["abnormal"] = False
            self._booted = True

    def seed_demo(self) -> dict[str, Any]:
        """构造续发样例：INSI-0003 邮件成功、短信超时（部分完成），等待续发与换版验收。"""
        self.bootstrap()
        with self._meta_lock:
            report = self._find_report_by_code_locked("INSI-0003")
            if report is None:
                raise ValueError("种子数据缺少 INSI-0003，无法构造续发样例")
            # 清掉该报告可能已有的调度痕迹，保证样例起点固定
            self.artifacts = [a for a in self.artifacts if a["报告编号"] != "INSI-0003"]
            self.receipts = [r for r in self.receipts if r["报告编号"] != "INSI-0003"]
            self.runs = [r for r in self.runs if r["报告编号"] != "INSI-0003"]
            # 期望组合按产物版本登记，该报告的产物已全部移除，对应登记一并清空
            surviving_versions = {a["产物版本"] for a in self.artifacts}
            self._expected_keys = {
                version: pairs
                for version, pairs in self._expected_keys.items()
                if version in surviving_versions
            }
            report["报告版本"] = None
            report["生成版本"] = None
            report["推送状态"] = REPORT_PUSH_NONE
            report["待补渠道"] = []
            report["失败原因"] = {}
        self.gateway.fix_outcome("INSI-0003", "邮件", GATEWAY_OK)
        self.gateway.fix_outcome("INSI-0003", "短信", GATEWAY_TIMEOUT)
        return self.push(report["id"], scheduler="seed-demo")

    # ------------------------------------------------------------------
    # 报告与产物
    # ------------------------------------------------------------------
    def list_reports(self) -> list[dict[str, Any]]:
        self.bootstrap()
        with self._meta_lock:
            return [dict(row) for row in store.rows(INSIGHT_MODULE)]

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        self.bootstrap()
        with self._meta_lock:
            row = store.find(INSIGHT_MODULE, report_id)
            return dict(row) if row else None

    def _find_report_locked(self, report_id: int) -> dict[str, Any]:
        row = store.find(INSIGHT_MODULE, report_id)
        if row is None:
            raise ValueError(f"洞察报告 {report_id} 不存在或已归档")
        return row

    def _find_report_by_code_locked(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(INSIGHT_MODULE):
            if row.get("报告编号") == code:
                return row
        return None

    def current_artifact(self, report_code: str) -> dict[str, Any] | None:
        with self._meta_lock:
            for artifact in reversed(self.artifacts):
                if artifact["报告编号"] == report_code and artifact["状态"] == "当前":
                    return dict(artifact)
        return None

    def generate_artifact(
        self,
        report_id: int,
        *,
        channels: list[str] | None = None,
        scopes: list[str] | None = None,
        segment_count: int = DEFAULT_SEGMENTS,
        change_report_version: bool = False,
        note: str = "",
        scheduler: str = "manual",
    ) -> tuple[dict[str, Any], Counter]:
        """生成（或换版）报表产物。

        换版时旧版本回执全部失效（计入失效数）；报告版本仅在报告内容变更时递增，
        产物版本每次生成都递增，二者与报告上记录的生成版本保持一致。
        """
        self.bootstrap()
        channels = [c for c in (channels or DEFAULT_CHANNELS) if str(c).strip()]
        scopes = [s for s in (scopes or DEFAULT_SCOPES) if str(s).strip()]
        segment_count = max(1, int(segment_count))
        now = self._now()
        counter = Counter()

        with self._meta_lock:
            report = self._find_report_locked(report_id)
            report_code = str(report["报告编号"])
            previous = next(
                (a for a in reversed(self.artifacts) if a["报告编号"] == report_code and a["状态"] == "当前"),
                None,
            )

            if previous is None:
                artifact_no = 1
                report_version = "r1"
            else:
                artifact_no = int(str(previous["产物版本"]).lstrip("v")) + 1
                previous["状态"] = "历史"
                previous["失效时间"] = now
                # 旧产物版本下仍生效的回执整体转为历史，不参与新版本推送状态
                for receipt in self.receipts:
                    if (
                        receipt["报告编号"] == report_code
                        and receipt["产物版本"] == previous["产物版本"]
                        and receipt["生效"]
                    ):
                        receipt["生效"] = False
                        receipt["历史"] = True
                        receipt["回执状态"] = RECEIPT_SUPERSEDED
                        receipt["失效时间"] = now
                        receipt["失效原因"] = f"报表产物换版至 v{artifact_no}，旧回执仅作历史"
                        counter.add_stale()
                prev_r = int(str(previous["报告版本"]).lstrip("r"))
                report_version = f"r{prev_r + 1 if change_report_version else prev_r}"

            artifact_version = f"v{artifact_no}"
            artifact = {
                "id": self._next_id_locked(self.artifacts),
                "产物编号": f"ART-{report_code}-{artifact_version}",
                "报告编号": report_code,
                "产物版本": artifact_version,
                "报告版本": report_version,
                # 洞察报告、报表产物、渠道回执共享同一个生成版本
                "生成版本": artifact_version,
                "渠道配置": channels,
                "接收范围": scopes,
                "每渠道分段数": segment_count,
                "生成时间": now,
                "状态": "当前",
                "备注": note,
            }
            self.artifacts.append(artifact)

            report["报告版本"] = report_version
            report["生成版本"] = artifact_version
            report["渠道配置"] = channels
            # 换版后推送状态重新开始，旧结果不沿用
            report["推送状态"] = REPORT_PUSH_NONE
            report["待补渠道"] = []
            report["失败原因"] = {}
            report["status"] = "生成中"
            report["pending"] = True

        if counter.stale:
            self._record_run(
                report_code=report_code,
                action="产物换版",
                artifact=artifact,
                counter=counter,
                channel_results=[],
                scheduler=scheduler,
                note=note,
            )
        return artifact, counter

    # ------------------------------------------------------------------
    # 推送与续发
    # ------------------------------------------------------------------
    def push(
        self,
        report_id: int,
        *,
        channels: list[str] | None = None,
        scopes: list[str] | None = None,
        segment_count: int | None = None,
        scheduler: str = "manual",
        overrides: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """按渠道+接收范围推送当前产物；自动复用成功渠道、只续发失败缺口。"""
        self.bootstrap()
        for channel, outcomes in (overrides or {}).items():
            report = self.get_report(report_id)
            if report is not None:
                self.gateway.configure(str(report["报告编号"]), channel, outcomes)
        return self._dispatch(report_id, only_gaps=False, scheduler=scheduler,
                              channels=channels, scopes=scopes, segment_count=segment_count)

    def retry_gaps(self, report_id: int, *, scheduler: str = "manual") -> dict[str, Any]:
        """续发：只处理当前产物下仍有缺口的渠道，成功渠道原样保留。"""
        self.bootstrap()
        return self._dispatch(report_id, only_gaps=True, scheduler=scheduler)

    def _dispatch(
        self,
        report_id: int,
        *,
        only_gaps: bool,
        scheduler: str,
        channels: list[str] | None = None,
        scopes: list[str] | None = None,
        segment_count: int | None = None,
    ) -> dict[str, Any]:
        # 先在元锁里确定产物快照，避免推送途中换版导致版本错位
        with self._meta_lock:
            report = self._find_report_locked(report_id)
            report_code = str(report["报告编号"])
            artifact = next(
                (a for a in reversed(self.artifacts) if a["报告编号"] == report_code and a["状态"] == "当前"),
                None,
            )
            if artifact is None:
                chosen_channels = [c for c in (channels or DEFAULT_CHANNELS) if str(c).strip()]
                chosen_scopes = [s for s in (scopes or DEFAULT_SCOPES) if str(s).strip()]
                chosen_segments = max(1, int(segment_count or DEFAULT_SEGMENTS))
            else:
                chosen_channels = channels or artifact["渠道配置"]
                chosen_scopes = scopes or artifact["接收范围"]
                chosen_segments = segment_count or artifact["每渠道分段数"]

        if artifact is None:
            artifact, _ = self.generate_artifact(
                report_id,
                channels=chosen_channels,
                scopes=chosen_scopes,
                segment_count=chosen_segments,
                scheduler=scheduler,
            )

        artifact_version = artifact["产物版本"]
        report_version = artifact["报告版本"]
        # 登记本轮期望触达的全部渠道组合，空回执被丢弃时仍可追踪待补渠道
        with self._meta_lock:
            expected = self._expected_keys.setdefault(artifact_version, set())
            for channel in chosen_channels:
                for scope in chosen_scopes:
                    expected.add((channel, scope))
        counter = Counter()
        channel_results: list[dict[str, Any]] = []
        conflict_details: list[str] = []
        empty_channels: list[str] = []

        for channel in chosen_channels:
            for scope in chosen_scopes:
                key = dedup_key(artifact_version, report_version, channel, scope)
                if only_gaps:
                    # 续发边界：成功渠道在外层直接跳过，根本不进入键锁与网关调用
                    with self._meta_lock:
                        snapshot = list(self.receipts)
                    existing = next(
                        (r for r in snapshot if r["生效"] and receipt_key_of(r) == key),
                        None,
                    )
                    if existing is not None and existing["回执状态"] == RECEIPT_SUCCESS:
                        counter.add_hit()
                        channel_results.append({
                            "渠道": channel,
                            "接收范围": scope,
                            "处理结果": "命中复用",
                            "回执状态": RECEIPT_SUCCESS,
                            "说明": "成功回执已存在，续发跳过",
                            "回执编号": existing["id"],
                        })
                        continue
                key_lock = self._lock_for(key)
                # 标记的检查与置位必须在同一把键锁内原子完成；
                # 锁在发送期间释放，使并发任务能观察到进行中标记并记冲突，
                # 而不是排队等锁后再重复发送。
                with key_lock:
                    if key in self._in_progress:
                        counter.add_conflict()
                        conflict_details.append(f"{channel}/{scope} 正在被其他调度任务处理，本次跳过")
                        channel_results.append({
                            "渠道": channel,
                            "接收范围": scope,
                            "处理结果": "冲突跳过",
                            "说明": "并发调度命中进行中标记，未重复发送",
                        })
                        continue
                    self._in_progress.add(key)
                try:
                    outcome = self._process_key(
                        report=report,
                        artifact=artifact,
                        channel=channel,
                        scope=scope,
                        key=key,
                        counter=counter,
                    )
                finally:
                    with key_lock:
                        self._in_progress.discard(key)
                if outcome["空回执"]:
                    empty_channels.append(f"{channel}/{scope}")
                channel_results.append(outcome["摘要"])

        summary = self._aggregate_report(report, artifact, empty_channels=empty_channels)
        run = self._record_run(
            report_code=report_code,
            action="失败续发" if only_gaps else "渠道推送",
            artifact=artifact,
            counter=counter,
            channel_results=channel_results,
            scheduler=scheduler,
            conflict_details=conflict_details,
            empty_channels=empty_channels,
        )
        return {
            "执行记录": run,
            "报告推送状态": summary,
            "计数": counter.as_dict(),
        }

    def _process_key(
        self,
        *,
        report: dict[str, Any],
        artifact: dict[str, Any],
        channel: str,
        scope: str,
        key: tuple[str, str, str, str],
        counter: Counter,
    ) -> dict[str, Any]:
        """处理单个四元组：命中复用 / 缺口续发 / 全新发送。

        续发边界由调用方决定（only_gaps 时只遍历有缺口的键，成功键在外层就被跳过），
        本方法只负责单个四元组内的去重与分段补发。调用方已持有键锁。
        """
        report_code = str(report["报告编号"])
        artifact_version = artifact["产物版本"]
        total_segments = artifact["每渠道分段数"]
        with self._meta_lock:
            existing = next(
                (r for r in self.receipts if r["生效"] and receipt_key_of(r) == key),
                None,
            )

            if existing is not None and existing["回执状态"] == RECEIPT_SUCCESS:
                # 成功渠道复用：不触发任何网关调用
                counter.add_hit()
                return {
                    "空回执": False,
                    "摘要": {
                        "渠道": channel,
                        "接收范围": scope,
                        "处理结果": "命中复用",
                        "回执状态": RECEIPT_SUCCESS,
                        "说明": "成功回执已存在，未重复发送",
                        "回执编号": existing["id"],
                    },
                }

            if existing is None:
                receipt = self._new_receipt(report, artifact, channel, scope, total_segments)
                self.receipts.append(receipt)
                gap_indexes = list(range(1, total_segments + 1))
            else:
                receipt = existing
                receipt["尝试次数"] += 1
                gap_indexes = [
                    item["分段序号"] for item in receipt["分段明细"] if item["状态"] == SEGMENT_GAP
                ]
                if not gap_indexes:
                    # 没有缺口（理论上只有成功态会走到这，前面已拦截），保守按命中处理
                    counter.add_hit()
                    return {
                        "空回执": False,
                        "摘要": {
                            "渠道": channel,
                            "接收范围": scope,
                            "处理结果": "命中复用",
                            "回执状态": receipt["回执状态"],
                            "回执编号": receipt["id"],
                        },
                    }

        sent_responses: list[dict[str, Any]] = []
        empty_responses = 0
        for segment_no in gap_indexes:
            response = self.gateway.send_segment(
                report_code=report_code,
                channel=channel,
                recipient_scope=scope,
                segment_no=segment_no,
                artifact_version=artifact_version,
            )
            if response["outcome"] == GATEWAY_EMPTY:
                # 空回执不落地、不计成功：该分段保持缺口，只留失败原因
                empty_responses += 1
                self._apply_segment(receipt, segment_no, SEGMENT_GAP, response)
                continue
            sent_responses.append(response)
            counter.add_retry()
            if response["outcome"] == GATEWAY_OK:
                self._apply_segment(receipt, segment_no, SEGMENT_DELIVERED, response)
            else:
                self._apply_segment(receipt, segment_no, SEGMENT_GAP, response)

        # 整轮全是空回执：不保留回执记录，避免空回执污染推送状态
        had_any_delivery = any(item["状态"] == SEGMENT_DELIVERED for item in receipt["分段明细"])
        if not sent_responses and empty_responses and not had_any_delivery:
            with self._meta_lock:
                self.receipts.remove(receipt)
            return {
                "空回执": True,
                "摘要": {
                    "渠道": channel,
                    "接收范围": scope,
                    "处理结果": RECEIPT_EMPTY,
                    "回执状态": RECEIPT_EMPTY,
                    "说明": "渠道未返回任何回执内容，空回执未计入推送状态",
                },
            }

        self._finalize_receipt(receipt)
        return {
            "空回执": False,
            "摘要": {
                "渠道": channel,
                "接收范围": scope,
                "处理结果": "缺口续发" if existing is not None else "全新发送",
                "回执状态": receipt["回执状态"],
                "渠道结论": receipt.get("渠道结论"),
                "成功段数": receipt["成功段数"],
                "缺口段数": receipt["缺口段数"],
                "失败原因": receipt["失败原因"],
                "回执编号": receipt["id"],
            },
        }

    # ------------------------------------------------------------------
    # 回执与状态查询
    # ------------------------------------------------------------------
    def list_receipts(
        self,
        report_id: int,
        *,
        include_history: bool = False,
        channel: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        """分页查看回执。每个四元组只返回一条（分段在明细里），并防御性去重。"""
        self.bootstrap()
        report = self.get_report(report_id)
        if report is None:
            raise ValueError(f"洞察报告 {report_id} 不存在或已归档")
        report_code = str(report["报告编号"])

        seen_keys: set[tuple[str, str, str, str]] = set()
        unique: list[dict[str, Any]] = []
        for receipt in sorted(self._snapshot_receipts_locked(), key=lambda item: item["id"]):
            if receipt["报告编号"] != report_code:
                continue
            if not include_history and receipt["历史"]:
                continue
            if channel and receipt["渠道"] != channel:
                continue
            key = receipt_key_of(receipt)
            if key in seen_keys:  # 双保险：任何情况下同一条回执不重复显示
                continue
            seen_keys.add(key)
            unique.append(dict(receipt))

        total = len(unique)
        start = max(page - 1, 0) * size
        return {
            "items": unique[start:start + size],
            "total": total,
            "page": page,
            "size": size,
            "当前产物版本": report.get("生成版本"),
        }

    def get_status(self, report_id: int) -> dict[str, Any]:
        """报告推送全景：版本一致性、渠道结果、待补渠道、四类计数与历史回执。"""
        self.bootstrap()
        report = self.get_report(report_id)
        if report is None:
            raise ValueError(f"洞察报告 {report_id} 不存在或已归档")
        report_code = str(report["报告编号"])
        artifact = self.current_artifact(report_code)

        snapshot = self._snapshot_receipts_locked()
        active = [r for r in snapshot if r["报告编号"] == report_code and r["生效"]]
        history = [r for r in snapshot if r["报告编号"] == report_code and r["历史"]]
        # 待补渠道与失败原因以报告上的汇总快照为准：空回执渠道没有回执记录，
        # 只有推送当时的聚合能把它们保留下来。
        pending_channels = list(report.get("待补渠道") or [])

        # 版本一致性：报告与当前产物必须读到同一个生成版本与报告版本
        version_consistent = bool(
            artifact
            and report.get("生成版本") == artifact["生成版本"]
            and report.get("报告版本") == artifact["报告版本"]
        )

        # 计数：命中/补发来自执行记录累计，失效来自历史回执，冲突来自执行记录
        with self._meta_lock:
            runs = [r for r in self.runs if r["报告编号"] == report_code]
        totals = Counter()
        for run in runs:
            totals = totals.add(Counter.from_dict(run["计数"]))

        return {
            "报告": report,
            "当前产物": artifact,
            "版本一致": version_consistent,
            "推送状态": report.get("推送状态", REPORT_PUSH_NONE),
            "待补渠道": pending_channels,
            "渠道回执": [dict(r) for r in sorted(active, key=lambda item: item["id"])],
            "历史回执": [dict(r) for r in sorted(history, key=lambda item: item["id"])],
            "计数": totals.as_dict(),
            "执行记录": [dict(r) for r in sorted(runs, key=lambda item: item["id"], reverse=True)],
        }

    def list_runs(self, report_id: int) -> list[dict[str, Any]]:
        self.bootstrap()
        report = self.get_report(report_id)
        if report is None:
            raise ValueError(f"洞察报告 {report_id} 不存在或已归档")
        code = str(report["报告编号"])
        with self._meta_lock:
            return [dict(r) for r in reversed(self.runs) if r["报告编号"] == code]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _aggregate_report(
        self,
        report: dict[str, Any],
        artifact: dict[str, Any],
        *,
        empty_channels: list[str] | None = None,
    ) -> dict[str, Any]:
        """按当前产物版本的生效回执汇总报告级状态；部分失败保留部分完成，不全量重发。"""
        empty_channels = empty_channels or []
        report_code = str(report["报告编号"])
        snapshot = self._snapshot_receipts_locked()
        active = [
            r for r in snapshot
            if r["报告编号"] == report_code
            and r["产物版本"] == artifact["产物版本"]
            and r["生效"]
        ]
        success_keys = [r for r in active if r["回执状态"] == RECEIPT_SUCCESS]
        gap_keys = [r for r in active if r["回执状态"] != RECEIPT_SUCCESS]

        # 期望集合 = 产物配置 ∪ 实际发起过推送的组合（兜住空回执被丢弃的场景）
        expected = {
            (channel, scope)
            for channel in artifact["渠道配置"]
            for scope in artifact["接收范围"]
        }
        expected |= self._expected_keys.get(artifact["产物版本"], set())
        covered = {(r["渠道"], r["接收范围"]) for r in active}
        missing_expected = sorted(f"{c}/{s}" for c, s in expected - covered)
        pending = sorted({f"{r['渠道']}/{r['接收范围']}" for r in gap_keys}) + missing_expected
        pending = list(dict.fromkeys(pending))

        failure_reasons = {
            f"{r['渠道']}/{r['接收范围']}": r["失败原因"]
            for r in gap_keys
            if r.get("失败原因")
        }
        # 空回执被丢弃、没有回执可挂原因时，用本轮空回执清单补回可读原因
        for label in missing_expected:
            if label not in failure_reasons and label in empty_channels:
                failure_reasons[label] = {"回执": "渠道未返回任何回执内容，空回执未计入推送状态"}

        if success_keys and not pending:
            push_status, report_status = REPORT_PUSH_FULL, "已推送"
            abnormal = False
        elif success_keys or any(r["成功段数"] > 0 for r in active):
            # 至少一个渠道有送达分段：保留部分完成，待补渠道显式列出，绝不整体重发
            push_status, report_status = REPORT_PUSH_PARTIAL, "部分完成"
            abnormal = True
        else:
            # 没有任何渠道拿到送达（全部超时/报错），或连一条回执都没留下：整体失败
            push_status, report_status = REPORT_PUSH_FAILED, "推送失败"
            abnormal = True

        report["推送状态"] = push_status
        report["待补渠道"] = pending
        report["失败原因"] = failure_reasons
        report["推送渠道"] = "、".join(artifact["渠道配置"])
        report["status"] = report_status
        report["abnormal"] = abnormal
        report["pending"] = bool(pending)
        return {
            "推送状态": push_status,
            "待补渠道": pending,
            "失败原因": failure_reasons,
            "成功渠道数": len(success_keys),
            "渠道总数": len(expected),
        }

    def _new_receipt(
        self,
        report: dict[str, Any],
        artifact: dict[str, Any],
        channel: str,
        scope: str,
        total_segments: int,
    ) -> dict[str, Any]:
        now = self._now()
        return {
            "id": self._next_id_locked(self.receipts),
            "报告编号": report["报告编号"],
            "报告标题": report.get("报告标题"),
            "产物版本": artifact["产物版本"],
            "报告版本": artifact["报告版本"],
            "生成版本": artifact["生成版本"],
            "渠道": channel,
            "接收范围": scope,
            "回执状态": RECEIPT_PARTIAL,
            "生效": True,
            "历史": False,
            "分段总数": total_segments,
            "成功段数": 0,
            "缺口段数": total_segments,
            "分段明细": [
                {"分段序号": no, "状态": SEGMENT_GAP, "回执号": None, "失败原因": None, "发送时间": None}
                for no in range(1, total_segments + 1)
            ],
            "失败原因": {},
            "尝试次数": 1,
            "首次发送时间": now,
            "最近发送时间": now,
            "失效时间": None,
        }

    @staticmethod
    def _apply_segment(receipt: dict[str, Any], segment_no: int, status: str, response: dict[str, Any]) -> None:
        detail = next(item for item in receipt["分段明细"] if item["分段序号"] == segment_no)
        detail["状态"] = status
        detail["发送时间"] = response["发送时间"]
        if status == SEGMENT_DELIVERED:
            detail["回执号"] = response.get("回执号")
            detail["失败原因"] = None
        else:
            detail["回执号"] = None
            detail["失败原因"] = response.get("失败原因") or "未收到送达确认"
            receipt["失败原因"][f"分段{segment_no}"] = detail["失败原因"]
        receipt["最近发送时间"] = response["发送时间"]

    @staticmethod
    def _finalize_receipt(receipt: dict[str, Any]) -> None:
        delivered = [item for item in receipt["分段明细"] if item["状态"] == SEGMENT_DELIVERED]
        gaps = [item for item in receipt["分段明细"] if item["状态"] == SEGMENT_GAP]
        receipt["成功段数"] = len(delivered)
        receipt["缺口段数"] = len(gaps)
        if not gaps:
            receipt["回执状态"] = RECEIPT_SUCCESS
            receipt["失败原因"] = {}
            return
        # 仍有缺口的回执统一为「部分完成」（成功段数可以为 0）：
        # 报告层据此保留部分完成/全部失败口径，续发时按缺口分段补发，
        # 超时与错误的差异保留在分段失败原因里，每个渠道都有可见结果。
        receipt["回执状态"] = RECEIPT_PARTIAL
        reasons = " ".join(item.get("失败原因") or "" for item in gaps)
        receipt["渠道结论"] = RECEIPT_TIMEOUT if "超时" in reasons else RECEIPT_FAILED

    def _record_run(
        self,
        *,
        report_code: str,
        action: str,
        artifact: dict[str, Any],
        counter: Counter,
        channel_results: list[dict[str, Any]],
        scheduler: str,
        conflict_details: list[str] | None = None,
        empty_channels: list[str] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        with self._meta_lock:
            run_id = self._next_id_locked(self.runs)
            run = {
                "id": run_id,
                "执行编号": f"RUN-{report_code}-{run_id:04d}",
                "报告编号": report_code,
                "动作类型": action,
                "调度任务": scheduler,
                "产物版本": artifact["产物版本"],
                "报告版本": artifact["报告版本"],
                "生成版本": artifact["生成版本"],
                "执行时间": self._now(),
                "计数": counter.as_dict(),
                "渠道结果": channel_results,
                "冲突明细": conflict_details or [],
                "空回执渠道": empty_channels or [],
                "备注": note,
            }
            self.runs.append(run)
            return dict(run)

    def _lock_for(self, key: tuple[str, str, str, str]) -> threading.Lock:
        with self._meta_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _snapshot_receipts_locked(self) -> list[dict[str, Any]]:
        """在元锁保护下取回执快照，避免并发推送修改列表时遍历报错。"""
        with self._meta_lock:
            return list(self.receipts)

    def _next_id_locked(self, table: list[dict[str, Any]]) -> int:
        return max((int(row.get("id", 0)) for row in table), default=0) + 1

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


dispatch_engine = DispatchEngine()
