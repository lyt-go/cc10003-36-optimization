"""端到端 HTTP 验收脚本：用标准库 urllib 走真实 FastAPI 路由，不引第三方依赖。

演练顺序对应需求验收点：
1. 重置 -> 构造 INSI-0003 邮件成功/短信超时样例（部分完成）；
2. 续发只补短信，邮件命中复用；
3. 回执分页不重复；
4. 产物换版，旧回执失效仅作历史，新版本状态独立；
5. 并发两个推送任务，出现冲突计数但回执唯一；
6. 空回执不落地、推送状态可见待补渠道；
7. 命中/补发/冲突/失效四类计数全程可见。
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import os

BASE = os.environ.get("INSIGHT_BASE_URL", "http://127.0.0.1:8011/api/insight")
REPORT_ID = 3


def call(method: str, path: str, payload: dict | None = None) -> dict:
    # 查询串里可能有中文渠道名，统一做 URL 编码
    if "?" in path:
        base, query = path.split("?", 1)
        path = f"{base}?{urllib.parse.quote(query, safe='=&')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def assert_eq(actual, expected, label):
    status = "OK " if actual == expected else "FAIL"
    print(f"[{status}] {label}: {actual!r}")
    assert actual == expected, f"{label}: {actual!r} != {expected!r}"


print("== 1. 重置并构造续发样例 ==")
call("POST", "/dispatch/reset")
demo = call("POST", "/dispatch/demo")
assert_eq(demo["报告推送状态"]["推送状态"], "部分完成", "样例报告状态")
assert_eq(demo["报告推送状态"]["待补渠道"], ["短信/默认接收范围"], "待补渠道")
# 补发计数 = 实际发起的分段发送数：邮件 3 段成功 + 短信 3 段超时尝试
assert_eq(demo["计数"]["补发"], 6, "首轮分段发送数（含超时尝试）")

print("== 2. 查看推送全景（版本一致、失败原因可见）==")
status = call("GET", f"/{REPORT_ID}/dispatch")
assert_eq(status["版本一致"], True, "报告与产物版本一致")
assert_eq(status["当前产物"]["产物版本"], "v1", "当前产物版本")
channels = {r["渠道"]: r for r in status["渠道回执"]}
assert_eq(channels["邮件"]["回执状态"], "成功", "邮件回执")
assert_eq(channels["短信"]["回执状态"], "部分完成", "短信回执")
assert "超时" in " ".join(channels["短信"]["失败原因"].values()), "短信失败原因可见"

print("== 3. 回执分页不重复 ==")
p1 = call("GET", f"/{REPORT_ID}/receipts?page=1&size=1")
p2 = call("GET", f"/{REPORT_ID}/receipts?page=2&size=1")
ids = [r["id"] for r in p1["items"] + p2["items"]]
assert_eq(len(ids) == len(set(ids)), True, "分页回执无重复")
assert_eq(p1["total"], 2, "回执总数")

print("== 4. 续发只补短信（每轮只补发剩余缺口）==")
# 网关单次编排按分段消费：每轮 retry 只补一个缺口分段，邮件始终命中复用
retry = None
for _ in range(3):
    retry = call("POST", f"/{REPORT_ID}/retry", {"overrides": {"短信": [{"outcome": "ok"}]}})
    if retry["报告推送状态"]["推送状态"] == "全部完成":
        break
assert_eq(retry["报告推送状态"]["推送状态"], "全部完成", "续发后全部完成")
assert retry["计数"]["命中"] >= 1, "邮件命中复用"
print(f"[OK ] 末次续发计数: {retry['计数']}")

print("== 5. 重复推送全部命中、零补发 ==")
again = call("POST", f"/{REPORT_ID}/push")
assert_eq(again["计数"]["命中"], 2, "二次推送命中数")
assert_eq(again["计数"]["补发"], 0, "二次推送补发数")

print("== 6. 产物换版：旧回执失效仅作历史 ==")
art = call("POST", f"/{REPORT_ID}/artifact", {"change_report_version": True, "note": "http验收换版"})
assert_eq(art["产物"]["产物版本"], "v2", "新产物版本")
assert_eq(art["产物"]["报告版本"], "r2", "新报告版本")
assert_eq(art["计数"]["失效"], 2, "旧回执失效数")
active = call("GET", f"/{REPORT_ID}/receipts")
history = call("GET", f"/{REPORT_ID}/receipts?include_history=true")
assert_eq(active["total"], 0, "新版本生效回执为零")
assert_eq(history["total"], 2, "历史回执仍可查")
assert all(r["回执状态"] == "已失效" for r in history["items"]), "历史回执状态"

print("== 7. 新版本推送与旧结果隔离 ==")
pushed = call("POST", f"/{REPORT_ID}/push", {
    "overrides": {"短信": [{"outcome": "timeout"}, {"outcome": "timeout"}, {"outcome": "timeout"}]},
})
assert_eq(pushed["执行记录"]["产物版本"], "v2", "新推送绑定 v2")
new_status = call("GET", f"/{REPORT_ID}/dispatch")
assert_eq(new_status["推送状态"], "部分完成", "新版本独立状态")
assert_eq(new_status["待补渠道"], ["短信/默认接收范围"], "新版本待补渠道")
assert_eq(new_status["计数"]["失效"], 2, "累计失效计数保留")

print("== 8. 并发推送冲突计数、回执唯一 ==")
barrier = threading.Barrier(2)
def concurrent_push(_):
    barrier.wait()
    return call("POST", f"/{REPORT_ID}/push", {
        "channels": ["站内信"], "overrides": {"站内信": [{"outcome": "ok", "delay": 0.15}] * 3},
    })
with ThreadPoolExecutor(max_workers=2) as pool:
    concurrent = list(pool.map(concurrent_push, range(2)))
conflict = sum(r["计数"]["冲突"] for r in concurrent)
assert conflict >= 1, "并发产生冲突计数"
station = call("GET", f"/{REPORT_ID}/receipts?channel=站内信")
assert_eq(station["total"], 1, "站内信回执唯一")
print(f"[OK ] 并发冲突数: {conflict}")

print("== 9. 空回执不落地 ==")
empty = call("POST", f"/{REPORT_ID}/push", {
    "channels": ["企业微信"],
    "overrides": {"企业微信": [{"outcome": "empty"}] * 3},
})
assert_eq(empty["执行记录"]["空回执渠道"], ["企业微信/默认接收范围"], "空回执登记")
wecom = call("GET", f"/{REPORT_ID}/receipts?channel=企业微信")
assert_eq(wecom["total"], 0, "空回执不落地")
final_status = call("GET", f"/{REPORT_ID}/dispatch")
assert "企业微信/默认接收范围" in final_status["待补渠道"], "空回执渠道仍在待补列表"

print("== 10. 执行记录四类计数齐全 ==")
runs = call("GET", f"/{REPORT_ID}/runs")
for run in runs["items"]:
    for label in ("命中", "补发", "冲突", "失效"):
        assert label in run["计数"], f"执行记录缺少 {label}"
print(f"[OK ] 共 {runs['total']} 条执行记录，四类计数齐全")

print("\n全部 HTTP 验收通过 ✔")
