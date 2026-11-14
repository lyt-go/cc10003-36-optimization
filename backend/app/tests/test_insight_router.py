"""路由层测试：验证参数校验、错误口径与引擎编排结果。

说明：FastAPI 的 Query(...) 占位对象在「直接调用路由函数」时是真值，无法模拟查询串，
因此分页/历史参数的行为在引擎层测试；这里重点覆盖 404/400、空请求体与动作口径。
"""
from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.dispatch import dispatch_engine
from app.routers import insight as insight_router
from app.schemas import ArtifactPayload, EntryPayload, PushPayload


class InsightRouterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        dispatch_engine.reset()

    def test_action_route_returns_readable_failure(self) -> None:
        result = insight_router.run_action(9999, EntryPayload(values={"action": "推送报告"}))
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.message)

    def test_dispatch_endpoints_raise_404_for_missing_report(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            insight_router.push_report(9999, PushPayload())
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as exc:
            insight_router.dispatch_status(9999)
        self.assertEqual(exc.exception.status_code, 404)

    def test_push_without_body_is_allowed(self) -> None:
        result = insight_router.push_report(3, None)
        self.assertIn("报告推送状态", result)
        self.assertIn("计数", result)

    def test_artifact_then_retry_full_flow(self) -> None:
        artifact_resp = insight_router.generate_artifact(
            3,
            ArtifactPayload(channels=["邮件", "短信"], segment_count=2),
        )
        self.assertEqual(artifact_resp["产物"]["产物版本"], "v1")

        push_resp = insight_router.push_report(
            3,
            PushPayload(
                overrides={
                    "邮件": [{"outcome": "ok"}, {"outcome": "ok"}],
                    "短信": [{"outcome": "timeout"}, {"outcome": "timeout"}],
                }
            ),
        )
        self.assertEqual(push_resp["报告推送状态"]["推送状态"], "部分完成")

        retry_resp = insight_router.retry_report(
            3,
            PushPayload(overrides={"短信": [{"outcome": "ok"}, {"outcome": "ok"}]}),
        )
        self.assertEqual(retry_resp["报告推送状态"]["推送状态"], "全部完成")
        self.assertEqual(retry_resp["计数"]["命中"], 1)
        self.assertEqual(retry_resp["计数"]["补发"], 2)

    def test_receipt_pagination_rejects_oversized_page(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            insight_router.list_receipts(3, size=500)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_revision_marks_history_and_export_still_works(self) -> None:
        insight_router.generate_artifact(3, ArtifactPayload())
        insight_router.push_report(
            3,
            PushPayload(overrides={"邮件": [{"outcome": "ok"}] * 3,
                                   "短信": [{"outcome": "ok"}] * 3}),
        )
        revised = insight_router.generate_artifact(3, ArtifactPayload(change_report_version=True))
        self.assertEqual(revised["计数"]["失效"], 2)

        # 历史/生效口径在引擎层验证（路由仅做 Query 绑定）
        history = dispatch_engine.list_receipts(3, include_history=True)
        self.assertEqual(history["total"], 2)
        self.assertTrue(all(item["历史"] for item in history["items"]))
        active = dispatch_engine.list_receipts(3)
        self.assertEqual(active["total"], 0)

        exported = insight_router.export_entries()
        self.assertEqual(exported["module"], "insight")
        self.assertGreaterEqual(exported["total"], 3)

    def test_demo_and_reset_endpoints(self) -> None:
        demo = insight_router.seed_demo()
        self.assertEqual(demo["报告推送状态"]["推送状态"], "部分完成")
        reset_resp = insight_router.reset_dispatch()
        self.assertTrue(reset_resp["ok"])
        receipts = dispatch_engine.list_receipts(3)
        self.assertEqual(receipts["total"], 0)


if __name__ == "__main__":
    unittest.main()
