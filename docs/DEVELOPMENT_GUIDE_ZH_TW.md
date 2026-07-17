# 開發指南

模組責任：Route 驗證 HTTP；Service 管商業規則；Repository 管 SQL；Provider 管外部 API；Worker 管背景執行；domain 不依賴 Flask。禁止 Route 直接模型呼叫或重型影像處理。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check inktime tests scripts server.py analyze_photos.py
mypy inktime
pytest tests/unit tests/security tests/integration
python scripts/performance_100k.py
docker compose build
```

Migration 只能新增版本，已發布版本不可改寫。測試使用 Mock Provider，不依賴私人 NAS、真實 API 或完整照片。錯誤需有穩定錯誤碼；Log 不得包含 Secret。Feature Flag 預留但未正式功能不得以空 UI 假裝完成。
