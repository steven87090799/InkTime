# 開發指南

模組責任：Route 驗證 HTTP；Service 管商業規則；Repository 管 SQL；Provider 管外部 API；Worker 管背景執行；domain 不依賴 Flask。禁止 Route 直接模型呼叫或重型影像處理。

本 repository 以 GitHub Actions 作為測試、Docker、benchmark、韌體編譯與 hosted runtime 的權威環境。一般修改在本機只做靜態來源檢查與 patch 格式檢查；不要自行執行 `pytest`、Docker build／smoke、Playwright、benchmark 或 Arduino／PlatformIO compile。分支推送後由 source-owned impact planner 選擇 owner suites；Ready／`full-ci`／`main` 才跑完整模式。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check inktime tests scripts server.py analyze_photos.py
mypy inktime
git diff --check
python3 scripts/ci/canonical_plan.py --help
```

目前最高為 Migration 50；Migration 只能新增版本，已發布版本不可改寫。測試使用 Mock Provider，不依賴私人 NAS、真實 API 或完整照片。錯誤需有穩定錯誤碼；Log 不得包含 Secret。Feature Flag 預留但未正式功能不得以空 UI 假裝完成。PR heavy job 通常驗證 merge-ref，必須把 source-head provenance 與 merge-ref validation 分開描述。
