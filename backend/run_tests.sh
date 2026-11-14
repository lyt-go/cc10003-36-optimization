#!/usr/bin/env bash
# 洞察调度去重与续发：一键跑全部后端测试（仅依赖标准库 + 项目 venv）。
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/python -m unittest app.tests.test_dispatch app.tests.test_insight_router -v
