"""洞察调度去重与续发的验收测试。

覆盖需求点：
1. INSI-0003 邮件成功、短信超时 -> 部分完成并列出待补渠道，续发只补短信；
2. 四元组（产物版本+报告版本+渠道+接收范围）回执唯一，成功渠道命中复用不重发；
3. 多个调度任务并发处理同一报告 -> 冲突计数、无重复回执；
4. 分段回执不重复显示，分页稳定；
5. 空回执不落地、不计入推送状态；
6. 产物换版 -> 旧回执失效仅作历史，新推送状态不沿用旧结果；
7. 洞察报告与报表产物读取同一生成版本（版本一致性）；
8. 命中/补发/冲突/失效四类计数可见；
9. 每个渠道都有可见的结果与失败原因。

运行：cd backend && .venv/bin/python -m unittest app.tests.test_dispatch -v
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.dispatch import dispatch_engine
from app.dispatch.models import (
    GATEWAY_EMPTY,
    GATEWAY_ERROR,
    GATEWAY_OK,
    GATEWAY_TIMEOUT,
    RECEIPT_PARTIAL,
    RECEIPT_SUCCESS,
    RECEIPT_SUPERSEDED,
    REPORT_PUSH_FAILED,
    REPORT_PUSH_FULL,
    REPORT_PUSH_PARTIAL,
    SEGMENT_DELIVERED,
    dedup_key,
    receipt_key_of,
)
from app.store import store

REPORT_ID = 3  # 种子数据里的 INSI-0003


class DispatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        dispatch_engine.reset()

    def tearDown(self) -> None:
        dispatch_engine.reset()

    def _report(self) -> dict:
        return store.find("insight", REPORT_ID)  # type: ignore[return-value]

    def _fix(self, channel: str, outcome: str, *, delay: float = 0.0) -> None:
        dispatch_engine.gateway.fix_outcome("INSI-0003", channel, outcome, delay=delay)

    # ------------------------------------------------------------------
    # 场景一：INSI-0003 邮件成功、短信超时，重试只补短信
    # ------------------------------------------------------------------
    def test_partial_completion_then_retry_only_gap_channel(self) -> None:
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_TIMEOUT)

        first = dispatch_engine.push(REPORT_ID, scheduler="scheduler-A")

        # 报告停在部分完成，而不是整体失败
        self.assertEqual(first["报告推送状态"]["推送状态"], REPORT_PUSH_PARTIAL)
        self.assertEqual(first["报告推送状态"]["待补渠道"], ["短信/默认接收范围"])
        self.assertEqual(self._report()["status"], "部分完成")

        # 每个渠道都有可见结果，短信带失败原因
        status = dispatch_engine.get_status(REPORT_ID)
        by_channel = {r["渠道"]: r for r in status["渠道回执"]}
        self.assertEqual(by_channel["邮件"]["回执状态"], RECEIPT_SUCCESS)
        self.assertEqual(by_channel["短信"]["回执状态"], RECEIPT_PARTIAL)
        self.assertEqual(by_channel["短信"]["渠道结论"], "超时")
        self.assertTrue(by_channel["短信"]["失败原因"], "短信渠道必须可见失败原因")
        self.assertIn("超时", " ".join(by_channel["短信"]["失败原因"].values()))

        # 首轮：邮件 3 段送达，短信 3 段全缺口
        self.assertEqual(by_channel["邮件"]["成功段数"], 3)
        self.assertEqual(by_channel["邮件"]["缺口段数"], 0)
        self.assertEqual(by_channel["短信"]["成功段数"], 0)
        self.assertEqual(by_channel["短信"]["缺口段数"], 3)

        # 续发：短信恢复成功。邮件不做任何配置变更，验证它命中复用、不会被再次调用
        self._fix("短信", GATEWAY_OK)
        second = dispatch_engine.retry_gaps(REPORT_ID, scheduler="scheduler-A")

        self.assertEqual(second["报告推送状态"]["推送状态"], REPORT_PUSH_FULL)
        self.assertEqual(second["报告推送状态"]["待补渠道"], [])
        # 邮件命中复用（1 次命中），短信只补发 3 个缺口分段
        self.assertEqual(second["计数"]["命中"], 1)
        self.assertEqual(second["计数"]["补发"], 3)

        results = {r["渠道"]: r for r in second["执行记录"]["渠道结果"]}
        self.assertEqual(results["邮件"]["处理结果"], "命中复用")
        self.assertEqual(results["短信"]["处理结果"], "缺口续发")

        # 全程只产生两条回执（每渠道一条），邮件回执只发送过一轮
        receipts = dispatch_engine.list_receipts(REPORT_ID)["items"]
        self.assertEqual(len(receipts), 2)
        mail_receipt = next(r for r in receipts if r["渠道"] == "邮件")
        self.assertEqual(mail_receipt["尝试次数"], 1, "成功渠道不应被重复发送")

    def test_retry_does_not_resend_delivered_segments(self) -> None:
        """短信首轮送达前两段、第三段超时；续发只能补发第 3 段。"""
        self._fix("邮件", GATEWAY_OK)
        dispatch_engine.gateway.configure(
            "INSI-0003",
            "短信",
            [
                {"outcome": GATEWAY_OK},
                {"outcome": GATEWAY_OK},
                {"outcome": GATEWAY_TIMEOUT},
            ],
        )
        dispatch_engine.push(REPORT_ID)
        sms = next(
            r for r in dispatch_engine.list_receipts(REPORT_ID)["items"] if r["渠道"] == "短信"
        )
        self.assertEqual(sms["成功段数"], 2)
        self.assertEqual(sms["缺口段数"], 1)
        first_two_receipt_no = [
            item["回执号"] for item in sms["分段明细"] if item["状态"] == SEGMENT_DELIVERED
        ]

        self._fix("短信", GATEWAY_OK)
        result = dispatch_engine.retry_gaps(REPORT_ID)
        # 补发数严格等于缺口数 1，而不是整渠道 3 段
        self.assertEqual(result["计数"]["补发"], 1)

        sms = next(
            r for r in dispatch_engine.list_receipts(REPORT_ID)["items"] if r["渠道"] == "短信"
        )
        self.assertEqual(sms["回执状态"], RECEIPT_SUCCESS)
        kept = [item["回执号"] for item in sms["分段明细"] if item["分段序号"] in (1, 2)]
        self.assertEqual(kept, first_two_receipt_no, "已送达分段的回执号必须原样保留")
        self.assertEqual(
            next(item for item in sms["分段明细"] if item["分段序号"] == 3)["状态"],
            SEGMENT_DELIVERED,
        )

    # ------------------------------------------------------------------
    # 场景二：四元组回执唯一 + 重复推送命中复用
    # ------------------------------------------------------------------
    def test_dedup_key_uniqueness_and_repeated_push_hits(self) -> None:
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_OK)
        first = dispatch_engine.push(REPORT_ID)
        artifact_version = first["执行记录"]["产物版本"]
        report_version = first["执行记录"]["报告版本"]

        keys = [receipt_key_of(r) for r in dispatch_engine.list_receipts(REPORT_ID)["items"]]
        self.assertEqual(len(keys), len(set(keys)), "同一四元组不得出现两条回执")
        for key in keys:
            self.assertEqual(key, dedup_key(artifact_version, report_version, key[2], key[3]))

        # 再推一次：两个渠道都应命中，零补发、零网关调用
        second = dispatch_engine.push(REPORT_ID)
        self.assertEqual(second["计数"]["命中"], 2)
        self.assertEqual(second["计数"]["补发"], 0)
        self.assertEqual(len(dispatch_engine.list_receipts(REPORT_ID)["items"]), 2)

    def test_multiple_scopes_produce_distinct_receipts(self) -> None:
        """不同接收范围是不同四元组，各自一条回执、互不影响续发。"""
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_TIMEOUT)
        result = dispatch_engine.push(
            REPORT_ID, scopes=["华东区", "华北区"], channels=["邮件", "短信"]
        )
        receipts = dispatch_engine.list_receipts(REPORT_ID)["items"]
        self.assertEqual(len(receipts), 4)  # 2 渠道 × 2 范围
        keys = [receipt_key_of(r) for r in receipts]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            result["报告推送状态"]["待补渠道"],
            ["短信/华东区", "短信/华北区"],
        )

        self._fix("短信", GATEWAY_OK)
        retry = dispatch_engine.retry_gaps(REPORT_ID)
        self.assertEqual(retry["计数"]["命中"], 2)  # 两个邮件范围复用
        self.assertEqual(retry["计数"]["补发"], 6)  # 两个短信范围各 3 段
        self.assertEqual(retry["报告推送状态"]["推送状态"], REPORT_PUSH_FULL)

    # ------------------------------------------------------------------
    # 场景三：并发调度同一报告 -> 冲突拦截、回执唯一
    # ------------------------------------------------------------------
    def test_concurrent_schedulers_conflict_without_duplicate_receipts(self) -> None:
        # 单渠道报告 + 分段发送耗时，保证两个并发任务在同一四元组上正面相撞
        self._fix("邮件", GATEWAY_OK, delay=0.12)
        barrier = threading.Barrier(2)

        def run_push() -> dict:
            barrier.wait()
            return dispatch_engine.push(
                REPORT_ID, channels=["邮件"], scopes=["默认接收范围"], scheduler="concurrent-task"
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run_push(), range(2)))

        conflict_total = sum(r["计数"]["冲突"] for r in results)
        self.assertGreaterEqual(conflict_total, 1, "并发处理同一四元组必须产生冲突计数")
        # 冲突任务不会产生第二条回执，也不会产生任何补发
        self.assertEqual(len(dispatch_engine.list_receipts(REPORT_ID)["items"]), 1)
        receipt = dispatch_engine.list_receipts(REPORT_ID)["items"][0]
        self.assertEqual(receipt["成功段数"], 3, "被冲突跳过的任务不得重复发送分段")

        status = dispatch_engine.get_status(REPORT_ID)
        self.assertGreaterEqual(status["计数"]["冲突"], 1)
        conflict_runs = [
            detail for run in status["执行记录"] for detail in run["冲突明细"]
        ]
        self.assertTrue(conflict_runs, "冲突原因必须在执行记录里可见")

    # ------------------------------------------------------------------
    # 场景四：分段查看不重复 + 分页稳定
    # ------------------------------------------------------------------
    def test_segment_receipts_no_duplicate_across_pages(self) -> None:
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_OK)
        dispatch_engine.push(REPORT_ID, scopes=[f"范围{i}" for i in range(1, 4)])

        page_one = dispatch_engine.list_receipts(REPORT_ID, page=1, size=3)
        page_two = dispatch_engine.list_receipts(REPORT_ID, page=2, size=3)
        all_items = page_one["items"] + page_two["items"]
        ids = [item["id"] for item in all_items]
        self.assertEqual(len(ids), len(set(ids)), "分页之间不能重复显示同一条回执")
        self.assertEqual(page_one["total"], 6)  # 2 渠道 × 3 范围

        # 分段明细内嵌且每段只出现一次
        for receipt in all_items:
            seg_nos = [item["分段序号"] for item in receipt["分段明细"]]
            self.assertEqual(sorted(seg_nos), [1, 2, 3])
            self.assertEqual(len(seg_nos), len(set(seg_nos)))

    # ------------------------------------------------------------------
    # 场景五：空回执不落地、不计成功
    # ------------------------------------------------------------------
    def test_empty_receipt_is_not_persisted_nor_counted(self) -> None:
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_EMPTY)
        result = dispatch_engine.push(REPORT_ID)

        receipts = dispatch_engine.list_receipts(REPORT_ID)["items"]
        self.assertEqual(len(receipts), 1, "空回执渠道不应留下回执记录")
        self.assertEqual(receipts[0]["渠道"], "邮件")
        self.assertEqual(result["执行记录"]["空回执渠道"], ["短信/默认接收范围"])

        status = dispatch_engine.get_status(REPORT_ID)
        self.assertEqual(status["推送状态"], REPORT_PUSH_PARTIAL)
        self.assertIn("短信/默认接收范围", status["待补渠道"])

        # 后续短信恢复正常，仍可正常补发，不受空回执残留影响
        self._fix("短信", GATEWAY_OK)
        retry = dispatch_engine.retry_gaps(REPORT_ID)
        self.assertEqual(retry["报告推送状态"]["推送状态"], REPORT_PUSH_FULL)
        self.assertEqual(len(dispatch_engine.list_receipts(REPORT_ID)["items"]), 2)

    def test_explicit_error_keeps_reason_visible(self) -> None:
        """渠道明确报错时回执保留可读失败原因，与超时区分。"""
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_ERROR)
        dispatch_engine.push(REPORT_ID)
        sms = next(
            r for r in dispatch_engine.list_receipts(REPORT_ID)["items"] if r["渠道"] == "短信"
        )
        self.assertEqual(sms["回执状态"], RECEIPT_PARTIAL)
        self.assertEqual(sms["渠道结论"], "失败")
        self.assertTrue(sms["失败原因"])
        self.assertIn("渠道暂不可用", " ".join(sms["失败原因"].values()))

    # ------------------------------------------------------------------
    # 场景六：产物换版，旧回执失效仅作历史
    # ------------------------------------------------------------------
    def test_artifact_reversion_marks_old_receipts_history_only(self) -> None:
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_OK)
        dispatch_engine.push(REPORT_ID)
        self.assertEqual(len(dispatch_engine.list_receipts(REPORT_ID)["items"]), 2)

        # 换版（报告内容也变更 -> 报告版本同步递增）
        artifact, counter = dispatch_engine.generate_artifact(
            REPORT_ID, change_report_version=True, note="换版验收"
        )
        self.assertEqual(artifact["产物版本"], "v2")
        self.assertEqual(artifact["报告版本"], "r2")
        self.assertEqual(counter.stale, 2, "两条旧回执都应计入失效")

        # 默认视图看不到旧回执；带历史参数可以查，且状态为已失效
        self.assertEqual(dispatch_engine.list_receipts(REPORT_ID)["items"], [])
        history = dispatch_engine.list_receipts(REPORT_ID, include_history=True)["items"]
        self.assertEqual(len(history), 2)
        self.assertTrue(all(r["回执状态"] == RECEIPT_SUPERSEDED for r in history))
        self.assertTrue(all(r["历史"] and not r["生效"] for r in history))

        # 新版本推送状态从零开始，旧邮件成功不被误用
        status = dispatch_engine.get_status(REPORT_ID)
        self.assertEqual(status["渠道回执"], [])
        self.assertEqual(status["计数"]["失效"], 2)
        self.assertEqual(self._report()["推送状态"], "未推送")

        # 新版本：邮件成功、短信超时，生成 v2/r2 的新回执，与历史共存但互不串号
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_TIMEOUT)
        dispatch_engine.push(REPORT_ID)
        new_receipts = dispatch_engine.list_receipts(REPORT_ID)["items"]
        self.assertTrue(all(r["产物版本"] == "v2" and r["报告版本"] == "r2" for r in new_receipts))
        full = dispatch_engine.list_receipts(REPORT_ID, include_history=True)["items"]
        self.assertEqual(len(full), 4)
        keys = [receipt_key_of(r) for r in full]
        self.assertEqual(len(keys), len(set(keys)), "新旧版本回执四元组天然不同，互不冲突")

        # 续发样例的换版口径：旧邮件回执只作历史，不参与新版本推送状态
        pending = dispatch_engine.get_status(REPORT_ID)["待补渠道"]
        self.assertEqual(pending, ["短信/默认接收范围"])

    # ------------------------------------------------------------------
    # 场景七：报告、产物、回执共享生成版本
    # ------------------------------------------------------------------
    def test_report_artifact_and_receipts_share_generation_version(self) -> None:
        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_TIMEOUT)
        dispatch_engine.push(REPORT_ID)

        status = dispatch_engine.get_status(REPORT_ID)
        report = status["报告"]
        artifact = status["当前产物"]
        self.assertTrue(status["版本一致"])
        self.assertEqual(report["生成版本"], artifact["生成版本"])
        self.assertEqual(report["报告版本"], artifact["报告版本"])
        for receipt in status["渠道回执"]:
            self.assertEqual(receipt["生成版本"], artifact["生成版本"])
            self.assertEqual(receipt["产物版本"], artifact["产物版本"])
            self.assertEqual(receipt["报告版本"], artifact["报告版本"])

    # ------------------------------------------------------------------
    # 场景八：演示种子、整体计数与全部失败
    # ------------------------------------------------------------------
    def test_seed_demo_builds_partial_state_and_counters_visible(self) -> None:
        result = dispatch_engine.seed_demo()
        self.assertEqual(result["报告推送状态"]["推送状态"], REPORT_PUSH_PARTIAL)
        self.assertEqual(result["报告推送状态"]["待补渠道"], ["短信/默认接收范围"])

        self._fix("短信", GATEWAY_OK)
        retry = dispatch_engine.retry_gaps(REPORT_ID)
        self.assertEqual(retry["报告推送状态"]["推送状态"], REPORT_PUSH_FULL)
        totals = dispatch_engine.get_status(REPORT_ID)["计数"]
        self.assertGreaterEqual(totals["命中"], 1)
        self.assertGreaterEqual(totals["补发"], 3)
        for label in ("命中", "补发", "冲突", "失效"):
            self.assertIn(label, totals, "验收必须能看到四类计数")

    def test_all_failure_does_not_mark_partial_and_retry_recovers(self) -> None:
        """渠道全部超时时报告为全部失败；恢复后续发可达全部完成。"""
        self._fix("邮件", GATEWAY_TIMEOUT)
        self._fix("短信", GATEWAY_TIMEOUT)
        first = dispatch_engine.push(REPORT_ID)
        self.assertEqual(first["报告推送状态"]["推送状态"], REPORT_PUSH_FAILED)
        self.assertEqual(self._report()["status"], "推送失败")

        self._fix("邮件", GATEWAY_OK)
        self._fix("短信", GATEWAY_OK)
        retry = dispatch_engine.retry_gaps(REPORT_ID)
        self.assertEqual(retry["报告推送状态"]["推送状态"], REPORT_PUSH_FULL)
        self.assertEqual(retry["计数"]["补发"], 6)
        self.assertEqual(retry["计数"]["命中"], 0)

    def test_missing_report_raises_readable_error(self) -> None:
        with self.assertRaises(ValueError):
            dispatch_engine.push(9999)
        with self.assertRaises(ValueError):
            dispatch_engine.get_status(9999)


if __name__ == "__main__":
    unittest.main()
