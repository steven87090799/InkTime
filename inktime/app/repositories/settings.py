from __future__ import annotations

from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken

from inktime.app.db import Database
from inktime.app.core.security import register_secret
from inktime.app.domain.analysis.scoring import (
    DEFAULT_FAVORITE_BONUS,
    DEFAULT_RANKING_WEIGHTS,
    DEFAULT_SCORING_RULES,
)


SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "general.timezone": {
        "category": "一般設定",
        "default": "Asia/Taipei",
        "type": "string",
        "description": "歷史今日與排程使用的時區",
        "risk": "錯誤時區會造成跨日選片偏移",
        "restart": False,
    },
    "analysis.strategy": {
        "category": "分析設定",
        "default": "smart_two_stage",
        "type": "string",
        "description": "新工作的預設分析策略",
        "risk": "高品質策略成本較高",
        "choices": ["local", "low_cost", "smart_two_stage", "high_quality"],
        "restart": False,
    },
    "analysis.ai_mode": {
        "category": "AI 模式",
        "default": "top_candidates",
        "type": "string",
        "description": "預設僅分析本機候選分最高的前 N 張；關閉時絕不呼叫模型",
        "risk": "完整照片庫必須在工作建立時確認，並依年份或資料夾分批處理",
        "choices": ["off", "top_candidates", "eligible", "full_library", "on_demand"],
        "choice_labels": {
            "off": "關閉 AI",
            "top_candidates": "只分析前 N 張候選",
            "eligible": "分析所有 eligible",
            "full_library": "分析完整照片庫",
            "on_demand": "按需分析",
        },
        "restart": False,
    },
    "analysis.ai_top_n": {
        "category": "AI 模式",
        "default": 50,
        "type": "integer",
        "description": "前 N 張候選模式每次允許進入 AI 的最高本機候選數",
        "risk": "提高數值會增加 Token 與成本；不會影響已快取結果",
        "min": 1,
        "max": 10000,
        "restart": False,
    },
    "analysis.ai_daily_photo_limit": {
        "category": "AI 模式",
        "default": 50,
        "type": "integer",
        "description": "每日可實際送往 AI 的不同照片上限，快取命中不計入",
        "risk": "達到上限時會退回本機選片，隔日可續跑",
        "min": 1,
        "max": 100000,
        "restart": False,
    },
    "analysis.ai_monthly_photo_limit": {
        "category": "AI 模式",
        "default": 500,
        "type": "integer",
        "description": "每月可實際送往 AI 的不同照片上限",
        "risk": "達到上限時會退回本機選片，需調整後才會再送出",
        "min": 1,
        "max": 1000000,
        "restart": False,
    },
    "travel_bonus_enabled": {
        "category": "旅行加權",
        "default": True,
        "type": "boolean",
        "description": "在模型原始回憶等級之外，依 GPS 與地點稀有度加入獨立旅行排序分",
        "risk": "缺少 GPS 或可信國家資訊時不臆測跨國，該項加分為 0",
        "restart": False,
    },
    "home_latitude": {
        "category": "旅行加權",
        "default": 25.033,
        "type": "number",
        "description": "住家緯度，只用於距離計算，不會傳送給模型",
        "risk": "這是敏感位置資訊，僅限管理員設定",
        "min": -90,
        "max": 90,
        "restart": False,
    },
    "home_longitude": {
        "category": "旅行加權",
        "default": 121.5654,
        "type": "number",
        "description": "住家經度，只用於距離計算，不會傳送給模型",
        "risk": "這是敏感位置資訊，僅限管理員設定",
        "min": -180,
        "max": 180,
        "restart": False,
    },
    "home_radius_km": {
        "category": "旅行加權",
        "default": 60.0,
        "type": "number",
        "description": "住家半徑內不給旅行加分",
        "risk": "過小會放大日常外出加分",
        "min": 0,
        "max": 1000,
        "restart": False,
    },
    "travel_bonus_near": {
        "category": "旅行加權",
        "default": 2.0,
        "type": "number",
        "description": "離家 60 至 200 km 的旅行加分",
        "risk": "只影響最終排序，不修改模型原始回憶等級",
        "min": 0,
        "max": 20,
        "restart": False,
    },
    "travel_bonus_far": {
        "category": "旅行加權",
        "default": 4.0,
        "type": "number",
        "description": "離家 200 至 1,000 km 的旅行加分",
        "risk": "只影響最終排序，不修改模型原始回憶等級",
        "min": 0,
        "max": 20,
        "restart": False,
    },
    "foreign_country_bonus": {
        "category": "旅行加權",
        "default": 6.0,
        "type": "number",
        "description": "可信國家候選顯示跨國時的旅行加分",
        "risk": "未知國家不加分，避免模型臆測地標造成偏差",
        "min": 0,
        "max": 20,
        "restart": False,
    },
    "rare_location_bonus": {
        "category": "旅行加權",
        "default": 2.0,
        "type": "number",
        "description": "同一粗略 GPS 地點在照片庫中罕見時的額外加分",
        "risk": "GPS 漂移與缺失會降低這項判斷的可靠度",
        "min": 0,
        "max": 20,
        "restart": False,
    },
    "max_total_bonus": {
        "category": "旅行加權",
        "default": 8.0,
        "type": "number",
        "description": "旅行與罕見地點加分的總上限",
        "risk": "過高會壓過模型的語意與技術評分",
        "min": 0,
        "max": 40,
        "restart": False,
    },
    "location_rule_version": {
        "category": "旅行加權",
        "default": "travel-v1",
        "type": "string",
        "description": "旅行加分規則版本，保留舊照片的可追溯性",
        "risk": "修改版本名稱不會自動重算既有分析",
        "max_length": 80,
        "restart": False,
    },
    "analysis.stage_two_threshold": {
        "category": "分析設定",
        "default": 65,
        "type": "number",
        "description": "進入第二階段的回憶分數門檻",
        "risk": "數值越低，模型成本越高",
        "min": 0,
        "max": 100,
        "restart": False,
    },
    "analysis.prefilter_enabled": {
        "category": "本機預篩選",
        "default": True,
        "type": "boolean",
        "description": "呼叫模型前先用本機特徵排除截圖與明顯低品質照片",
        "risk": "最愛照片會略過預篩選；排除結果保留在照片分析紀錄且不會刪除原檔",
        "restart": False,
    },
    "analysis.prefilter_screenshots": {
        "category": "本機預篩選",
        "default": True,
        "type": "boolean",
        "description": "依檔名、圖片尺寸、格式與相機 EXIF 判定並排除截圖",
        "risk": "裁切成手機螢幕尺寸的相機照片可能被判定為截圖；可降低敏感度或停用",
        "restart": False,
    },
    "analysis.prefilter_low_quality": {
        "category": "本機預篩選",
        "default": True,
        "type": "boolean",
        "description": "至少同時出現兩項明顯缺陷時排除，例如模糊、過曝、欠曝、低對比或低解析度",
        "risk": "本機規則只能判斷技術缺陷，不能可靠理解構圖、人物或回憶價值",
        "restart": False,
    },
    "analysis.prefilter_sensitivity": {
        "category": "本機預篩選",
        "default": "conservative",
        "type": "string",
        "description": "預篩選敏感度：conservative 保守、balanced 平衡、aggressive 積極",
        "risk": "越積極可節省更多 Token，但誤排除夜景、極簡構圖或舊照片的機率越高",
        "choices": ["conservative", "balanced", "aggressive"],
        "restart": False,
    },
    "analysis.e6_prefilter_enabled": {
        "category": "本機預篩選",
        "default": True,
        "type": "boolean",
        "description": "呼叫模型前模擬 PhotoPainter 六色量化，排除在 E6 面板上嚴重失真的照片",
        "risk": "最愛照片會略過；這是電子紙適合度，不代表原始照片本身不好看",
        "restart": False,
    },
    "analysis.e6_min_score": {
        "category": "本機預篩選",
        "default": 25,
        "type": "number",
        "description": "E6 六色適合度低於此分數時，不呼叫模型",
        "risk": "建議先維持 20–35；提高門檻可節省 Token，但會排除更多色彩細膩的照片",
        "min": 0,
        "max": 100,
        "restart": False,
    },
    "analysis.scoring_rules": {
        "category": "照片評分規則",
        "default": DEFAULT_SCORING_RULES,
        "type": "string",
        "description": "模型判斷哪些照片應取得較高或較低分的規則",
        "risk": "修改後會影響新分析結果；既有照片分數不會自動重算",
        "restart": False,
        "multiline": True,
        "rows": 26,
        "min_length": 100,
        "max_length": 12000,
        "full_width": True,
        "control_center": True,
    },
    "analysis.ranking_memory_weight": {
        "category": "評分控制中心",
        "default": DEFAULT_RANKING_WEIGHTS["memory"],
        "type": "number",
        "description": "綜合排序分的回憶分權重",
        "risk": "四項權重合計必須為 100%",
        "min": 0,
        "max": 100,
        "restart": False,
        "control_center": True,
    },
    "analysis.ranking_beauty_weight": {
        "category": "評分控制中心",
        "default": DEFAULT_RANKING_WEIGHTS["beauty"],
        "type": "number",
        "description": "綜合排序分的美觀分權重",
        "risk": "四項權重合計必須為 100%",
        "min": 0,
        "max": 100,
        "restart": False,
        "control_center": True,
    },
    "analysis.ranking_technical_weight": {
        "category": "評分控制中心",
        "default": DEFAULT_RANKING_WEIGHTS["technical_quality"],
        "type": "number",
        "description": "綜合排序分的技術品質權重",
        "risk": "四項權重合計必須為 100%",
        "min": 0,
        "max": 100,
        "restart": False,
        "control_center": True,
    },
    "analysis.ranking_emotion_weight": {
        "category": "評分控制中心",
        "default": DEFAULT_RANKING_WEIGHTS["emotion"],
        "type": "number",
        "description": "綜合排序分的情緒分權重",
        "risk": "四項權重合計必須為 100%",
        "min": 0,
        "max": 100,
        "restart": False,
        "control_center": True,
    },
    "analysis.ranking_favorite_bonus": {
        "category": "評分控制中心",
        "default": DEFAULT_FAVORITE_BONUS,
        "type": "number",
        "description": "最愛照片加入綜合排序分的額外分數",
        "risk": "過高可能讓低品質最愛照片排在最前面",
        "min": 0,
        "max": 30,
        "restart": False,
        "control_center": True,
    },
    "analysis.concurrency": {
        "category": "分析設定",
        "default": 1,
        "type": "integer",
        "description": "Worker 最大並行數",
        "risk": "Intel N100 建議 1；過高可能觸發 Rate Limit 或造成圖片解碼記憶體尖峰",
        "min": 1,
        "max": 8,
        "restart": False,
    },
    "worker.queue_multiplier": {
        "category": "效能與待機",
        "default": 1,
        "type": "integer",
        "description": "每個並行槽預先排入記憶體的工作數",
        "risk": "數值越高吞吐可能略增，但同時保留更多圖片與 Future",
        "min": 1,
        "max": 4,
        "restart": False,
    },
    "worker.poll_seconds": {
        "category": "效能與待機",
        "default": 15,
        "type": "number",
        "description": "沒有工作時 Worker 檢查新工作的秒數",
        "risk": "數值越小反應越快，但會增加待機喚醒與 SQLite 讀取",
        "min": 1,
        "max": 300,
        "restart": False,
    },
    "worker.progress_items": {
        "category": "效能與待機",
        "default": 50,
        "type": "integer",
        "description": "每完成多少項目輸出一次彙總進度 Log",
        "risk": "設為太小會產生大量 Docker Log；不會逐張輸出",
        "min": 5,
        "max": 10000,
        "restart": False,
    },
    "worker.progress_seconds": {
        "category": "效能與待機",
        "default": 300,
        "type": "integer",
        "description": "工作進行時兩次彙總進度 Log 的最長秒數",
        "risk": "設為太小會增加 Log 量",
        "min": 30,
        "max": 3600,
        "restart": False,
    },
    "scanner.disk_batch_size": {
        "category": "效能與待機",
        "default": 1000,
        "type": "integer",
        "description": "照片掃描每批從磁碟送入比對的路徑數",
        "risk": "N100／16 GB 建議 1,000；提高會增加單批記憶體，降低會增加 SQL 次數",
        "min": 100,
        "max": 10000,
        "restart": False,
    },
    "scanner.write_batch_size": {
        "category": "效能與待機",
        "default": 500,
        "type": "integer",
        "description": "Scanner 單一 SQLite 批次交易的照片數",
        "risk": "建議 500；提高會延長單次 writer 鎖時間",
        "min": 100,
        "max": 2000,
        "restart": False,
    },
    "scanner.missing_threshold_percent": {
        "category": "效能與待機",
        "default": 10,
        "type": "number",
        "description": "單次完整掃描可自動標記 Missing 的最大照片比例（%）",
        "risk": "超過此比例會停止更新並要求管理員確認；不建議高於 10%",
        "min": 0,
        "max": 100,
        "restart": False,
    },
    "scheduler.poll_seconds": {
        "category": "效能與待機",
        "default": 60,
        "type": "integer",
        "description": "Scheduler 檢查備份與逾期租約的秒數",
        "risk": "數值越小會增加待機喚醒；不建議低於 30 秒",
        "min": 30,
        "max": 3600,
        "restart": False,
    },
    "analysis.max_retries": {
        "category": "分析設定",
        "default": 3,
        "type": "integer",
        "description": "單一項目最大嘗試次數",
        "risk": "重試會增加成本",
        "min": 0,
        "max": 10,
        "restart": False,
    },
    "model.low_model": {
        "category": "模型設定",
        "default": "gpt-4o-mini",
        "type": "string",
        "description": "第一階段低成本模型",
        "risk": "模型必須支援圖片與 JSON Schema",
        "restart": False,
    },
    "model.high_model": {
        "category": "模型設定",
        "default": "gpt-4o",
        "type": "string",
        "description": "第二階段高品質模型",
        "risk": "請確認 Provider 價格",
        "restart": False,
    },
    "budget.daily_warning": {
        "category": "成本設定",
        "default": 5.0,
        "type": "number",
        "description": "每日成本警告值（美元）",
        "risk": "只警告，不會停止",
        "min": 0,
        "max": 100000,
        "restart": False,
    },
    "budget.daily_stop": {
        "category": "成本設定",
        "default": 10.0,
        "type": "number",
        "description": "每日成本停止值（美元）",
        "risk": "達到後新模型請求會暫停",
        "min": 0,
        "max": 100000,
        "restart": False,
    },
    "budget.monthly_warning": {
        "category": "成本設定",
        "default": 50.0,
        "type": "number",
        "description": "每月成本警告值（美元）",
        "risk": "只警告，不會停止",
        "min": 0,
        "max": 1000000,
        "restart": False,
    },
    "budget.monthly_stop": {
        "category": "成本設定",
        "default": 100.0,
        "type": "number",
        "description": "每月成本停止值（美元）",
        "risk": "達到後新模型請求會暫停",
        "min": 0,
        "max": 1000000,
        "restart": False,
    },
    "budget.job_default": {
        "category": "成本設定",
        "default": 10.0,
        "type": "number",
        "description": "單一工作的預設預算",
        "risk": "工作達到後自動暫停",
        "min": 0,
        "max": 100000,
        "restart": False,
    },
    "budget.photo_max": {
        "category": "成本設定",
        "default": 0.25,
        "type": "number",
        "description": "單張照片成本上限",
        "risk": "過低可能使高品質分析無法執行",
        "min": 0,
        "max": 1000,
        "restart": False,
    },
    "budget.max_tokens": {
        "category": "成本設定",
        "default": 8000,
        "type": "integer",
        "description": "單次請求最大 Token 估算上限",
        "risk": "需與 Provider 模型能力相容",
        "min": 256,
        "max": 1000000,
        "restart": False,
    },
    "render.memory_threshold": {
        "category": "渲染設定",
        "default": 70,
        "type": "number",
        "description": "歷史今日選片最低回憶分數",
        "risk": "過高可能沒有候選照片",
        "min": 0,
        "max": 100,
        "restart": False,
    },
    "render.quantity": {
        "category": "渲染設定",
        "default": 5,
        "type": "integer",
        "description": "每次發布畫面數",
        "risk": "增加數量會增加裝置下載量；雙照片拼版每個畫面會使用兩張候選照片",
        "min": 1,
        "max": 50,
        "restart": False,
    },
    "render.selection_mode": {
        "category": "渲染設定",
        "default": "history_today",
        "type": "string",
        "description": "自動發布的選片方式；history_today 先選歷年同月同日",
        "risk": "照片缺少拍攝日期時只能依回退規則補足",
        "choices": ["history_today", "top_ranked"],
        "restart": False,
    },
    "render.history_today_window_days": {
        "category": "渲染設定",
        "default": 7,
        "type": "integer",
        "description": "歷年同日不足時，可向前後尋找的日數",
        "risk": "設為 0 只接受完全相同月日",
        "min": 0,
        "max": 31,
        "restart": False,
    },
    "render.history_today_fallback": {
        "category": "渲染設定",
        "default": "nearby_then_ranked",
        "type": "string",
        "description": "歷年同日數量不足時的補圖規則",
        "risk": "none 可能產生沒有照片可發布的工作",
        "choices": ["nearby_then_ranked", "nearby_only", "ranked", "none"],
        "restart": False,
    },
    "render.e6_weight": {
        "category": "渲染設定",
        "default": 20,
        "type": "number",
        "description": "自動選片時 E6 六色適合度占最終排序的百分比",
        "risk": "過高會讓面板顯示效果凌駕回憶與情緒分數",
        "min": 0,
        "max": 60,
        "restart": False,
    },
    "render.layout": {
        "category": "渲染設定",
        "default": "photo_info",
        "type": "string",
        "description": "正式發布使用的相框版型",
        "risk": "日曆與天氣版型會縮小照片顯示區域",
        "choices": [
            "full",
            "postcard",
            "photo_info",
            "photo_pair",
            "adaptive_memory",
            "calendar",
            "weather_sensor",
        ],
        "choice_labels": {
            "full": "單張照片",
            "postcard": "明信片",
            "photo_info": "單張照片＋資訊",
            "photo_pair": "雙照片拼版",
            "adaptive_memory": "智慧自適應回憶（建議）",
            "calendar": "月曆相框（直向）",
            "weather_sensor": "天氣與室內溫溼度（直向）",
        },
        "restart": False,
    },
    "render.frame_orientation": {
        "category": "渲染設定",
        "default": "portrait",
        "type": "string",
        "description": "相框實際擺放方向",
        "risk": "月曆與天氣版型固定使用直向；其他版型會依此重新排版",
        "choices": ["portrait", "landscape"],
        "choice_labels": {"portrait": "直向", "landscape": "橫向"},
        "restart": False,
    },
    "render.fit_mode": {
        "category": "渲染設定",
        "default": "contain",
        "type": "string",
        "description": "照片縮放方式",
        "risk": "完整顯示不裁切照片；填滿畫面可能裁掉照片邊緣",
        "choices": ["contain", "cover"],
        "choice_labels": {"contain": "完整顯示（建議）", "cover": "填滿並裁切"},
        "restart": False,
    },
    "render.show_capture_date": {
        "category": "渲染設定",
        "default": True,
        "type": "boolean",
        "description": "在支援文字的版型顯示照片拍攝日期",
        "risk": "照片 EXIF 日期錯誤時會顯示錯誤日期，可在照片詳情人工修正",
        "restart": False,
    },
    "render.weather_enabled": {
        "category": "相框天氣與感測",
        "default": False,
        "type": "boolean",
        "description": "天氣版型從 Open-Meteo 取得所在地目前天氣與今日高低溫",
        "risk": "需要 Worker 可連外；失敗時仍會發布，但畫面標示天氣暫時無法取得",
        "restart": False,
    },
    "render.weather_latitude": {
        "category": "相框天氣與感測",
        "default": 25.033,
        "type": "number",
        "description": "天氣位置緯度（預設臺北市中心，啟用前請修改）",
        "risk": "位置不正確會取得錯誤天氣",
        "min": -90,
        "max": 90,
        "restart": False,
    },
    "render.weather_longitude": {
        "category": "相框天氣與感測",
        "default": 121.5654,
        "type": "number",
        "description": "天氣位置經度（預設臺北市中心，啟用前請修改）",
        "risk": "位置不正確會取得錯誤天氣",
        "min": -180,
        "max": 180,
        "restart": False,
    },
    "render.weather_location_name": {
        "category": "相框天氣與感測",
        "default": "所在地",
        "type": "string",
        "description": "天氣版型顯示的地點名稱",
        "risk": "只影響畫面標示，不會自動修改經緯度",
        "restart": False,
    },
    "render.sensor_device_id": {
        "category": "相框天氣與感測",
        "default": "",
        "type": "string",
        "description": "室內溫溼度使用的裝置 ID；留空時採用最近回報的裝置",
        "risk": "多台裝置時建議指定，避免顯示另一個房間的感測值",
        "restart": False,
    },
    "render.font_path": {
        "category": "渲染設定",
        "default": "builtin:iansui",
        "type": "string",
        "description": "繁體中文字型；可選內建手寫／文青風格或管理員上傳字型",
        "risk": "正式渲染會逐段檢查字元，缺字時禁止發布且不使用預設字型替代",
        "restart": False,
    },
    "render.show_location": {
        "category": "渲染設定",
        "default": True,
        "type": "boolean",
        "description": "照片含 GPS 時，在電子紙短文案下方顯示最近城市",
        "risk": "只顯示離線城市索引的粗略地名，不顯示精確座標；停用可從電子紙畫面隱藏",
        "restart": False,
    },
    "render.location_max_distance_km": {
        "category": "渲染設定",
        "default": 80,
        "type": "number",
        "description": "GPS 距離最近城市在此公里數內才顯示地名",
        "risk": "數值過大可能顯示不夠準確的鄰近城市",
        "min": 1,
        "max": 500,
        "restart": False,
    },
    "render.profile": {
        "category": "渲染設定",
        "default": "safe_4c",
        "type": "string",
        "description": "正式發布使用的電子紙面板色彩 Profile",
        "risk": "必須與裝置頁設定的面板型號一致；不一致時韌體會拒絕更新",
        "choices": ["safe_4c", "gdep073e01_6c", "gdey073d46_7c"],
        "restart": False,
    },
    "render.dither": {
        "category": "渲染設定",
        "default": "floyd_steinberg",
        "type": "string",
        "description": "有限色電子紙的抖動算法；可選 GDEP 原廠相容或減少色塊與雜點的照片平滑模式",
        "risk": "原廠相容與照片平滑固定使用標準強度；照片平滑可能略微柔化極細線條",
        "choices": [
            "none",
            "floyd_steinberg",
            "gooddisplay",
            "photo_smooth",
            "atkinson",
            "bayer4",
            "bayer8",
            "nearest",
            "bayer_ordered",
            "serpentine_floyd_steinberg",
        ],
        "choice_labels": {
            "none": "不抖動",
            "floyd_steinberg": "Floyd–Steinberg（InkTime）",
            "gooddisplay": "Good Display 原廠相容",
            "photo_smooth": "照片平滑（減少色塊／雜點）",
            "atkinson": "Atkinson（較柔和）",
            "bayer4": "Bayer 4×4（規律顆粒）",
            "bayer8": "Bayer 8×8（細緻顆粒）",
            "nearest": "最近色（新 Renderer）",
            "bayer_ordered": "Bayer Ordered（新 Renderer）",
            "serpentine_floyd_steinberg": "蛇形 Floyd–Steinberg（新 Renderer）",
        },
        "restart": False,
    },
    "render.dither_strength": {
        "category": "渲染設定",
        "default": 1.0,
        "type": "number",
        "description": "抖動誤差或閾值強度；0 為關閉、1 為標準；原廠相容與照片平滑固定為 1",
        "risk": "過高會增加顆粒與色點；Good Display 原廠相容與照片平滑不使用此值",
        "min": 0,
        "max": 2,
        "restart": False,
    },
    "render.color_distance": {
        "category": "渲染設定",
        "default": "oklab",
        "type": "string",
        "description": "一般算法的色盤映射距離；OKLab 較符合人眼感知，RGB 較接近舊版",
        "risk": "照片平滑與原廠相容固定使用 RGB；其他模式切換後色彩分布會不同",
        "choices": ["oklab", "rgb"],
        "restart": False,
    },
    "render.custom_photo_presets": {
        "category": "渲染設定",
        "default": "{}",
        "type": "string",
        "description": "六色照片 Renderer 的自訂 Preset（由 A/B 預覽頁管理）",
        "risk": "內建 Preset 不會被覆寫；修改內建值時會另存自訂副本",
        "max_length": 50000,
        "restart": False,
    },
    "device.legacy_api_enabled": {
        "category": "裝置設定",
        "default": False,
        "type": "boolean",
        "description": "舊版 URL 金鑰下載模式",
        "risk": "不安全；Token 可能進入 URL 與 Log",
        "restart": True,
    },
    "device.default_timezone": {
        "category": "裝置設定",
        "default": "Asia/Taipei",
        "type": "string",
        "description": "新增 ESP32 裝置時套用的 IANA 時區",
        "risk": "錯誤時區會使每日喚醒時間偏移",
        "restart": False,
    },
    "device.default_schedule": {
        "category": "裝置設定",
        "default": "08:00",
        "type": "string",
        "description": "新增 ESP32 裝置時套用的每日刷新時間（HH:MM）",
        "risk": "刷新時電子紙與 Wi-Fi 會短暫提高耗電",
        "pattern": "time_hhmm",
        "restart": False,
    },
    "device.default_rotation": {
        "category": "裝置設定",
        "default": 0,
        "type": "integer",
        "description": "新增 7.3 吋裝置時套用的畫面方向",
        "risk": "目前正式韌體只支援 0° 與 180°",
        "choices": [0, 180],
        "restart": False,
    },
    "device.default_panel_profile": {
        "category": "裝置設定",
        "default": "safe_4c",
        "type": "string",
        "description": "新增 ESP32 裝置時套用的面板 Profile",
        "risk": "請依實際面板型號選擇；選錯會由韌體安全拒絕",
        "choices": ["safe_4c", "gdep073e01_6c", "gdey073d46_7c"],
        "restart": False,
    },
    "notification.device_offline_enabled": {
        "category": "裝置通知",
        "default": True,
        "type": "boolean",
        "description": "裝置超過門檻未連線時建立站內通知",
        "risk": "若裝置刷新週期長於離線門檻會誤報",
        "restart": False,
    },
    "notification.device_offline_hours": {
        "category": "裝置通知",
        "default": 30,
        "type": "number",
        "description": "距離最後連線多久後判定離線（小時）",
        "risk": "每日喚醒裝置建議至少 26–30 小時",
        "min": 1,
        "max": 720,
        "restart": False,
    },
    "notification.device_recovery_enabled": {
        "category": "裝置通知",
        "default": True,
        "type": "boolean",
        "description": "曾離線的裝置重新回報後建立恢復通知",
        "risk": "停用後仍會清除離線狀態，但不建立恢復訊息",
        "restart": False,
    },
    "notification.device_offline_repeat_enabled": {
        "category": "裝置通知",
        "default": False,
        "type": "boolean",
        "description": "離線期間是否依冷卻時間重複提醒",
        "risk": "啟用可能增加外部 Webhook 通知量",
        "restart": False,
    },
    "notification.device_offline_cooldown_hours": {
        "category": "裝置通知",
        "default": 24,
        "type": "number",
        "description": "同一裝置重複離線提醒的最短間隔（小時）",
        "risk": "過短會造成通知轟炸",
        "min": 1,
        "max": 720,
        "restart": False,
    },
    "notification.scan_seconds": {
        "category": "裝置通知",
        "default": 300,
        "type": "integer",
        "description": "Scheduler 掃描裝置離線與恢復狀態的秒數",
        "risk": "過短會增加待機 SQLite 讀取",
        "min": 60,
        "max": 3600,
        "restart": False,
    },
    "notification.webhook_enabled": {
        "category": "裝置通知",
        "default": False,
        "type": "boolean",
        "description": "將新通知以 JSON POST 到外部 Webhook",
        "risk": "Webhook 端點會收到裝置名稱與狀態；Token 另以加密欄位保存",
        "restart": False,
    },
    "notification.webhook_url": {
        "category": "裝置通知",
        "default": "",
        "type": "string",
        "description": "接收通知的完整 http:// 或 https:// URL",
        "risk": "管理員可設定內網端點；請只使用可信服務",
        "pattern": "optional_http_url",
        "max_length": 2048,
        "restart": False,
    },
    "notification.webhook_timeout_seconds": {
        "category": "裝置通知",
        "default": 10,
        "type": "integer",
        "description": "Webhook 單次連線與回應逾時秒數",
        "risk": "過長會延後 Scheduler 的其他低頻工作",
        "min": 2,
        "max": 30,
        "restart": False,
    },
    "system.log_level": {
        "category": "Log 與診斷",
        "default": "INFO",
        "type": "string",
        "description": "應用程式最低 Log 層級；建議正式環境使用 INFO 或 WARNING",
        "risk": "DEBUG 可能包含大量技術細節並增加磁碟寫入",
        "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "restart": False,
    },
    "system.log_format": {
        "category": "Log 與診斷",
        "default": "json",
        "type": "string",
        "description": "Console Log 格式（human/json）",
        "risk": "JSON 適合集中式 Log",
        "choices": ["human", "json"],
        "restart": False,
    },
    "system.diagnostics_cache_seconds": {
        "category": "Log 與診斷",
        "default": 300,
        "type": "integer",
        "description": "診斷頁重算縮圖目錄大小前的快取秒數",
        "risk": "數值太小會在大型快取目錄產生頻繁磁碟掃描",
        "min": 30,
        "max": 86400,
        "restart": False,
    },
    "security.session_minutes": {
        "category": "安全設定",
        "default": 30,
        "type": "integer",
        "description": "管理介面閒置 Session 分鐘數",
        "risk": "過長會增加共用裝置風險",
        "min": 5,
        "max": 1440,
        "restart": False,
    },
    "backup.schedule_enabled": {
        "category": "備份設定",
        "default": True,
        "type": "boolean",
        "description": "每日自動建立資料庫備份",
        "risk": "需預留備份空間",
        "restart": False,
    },
    "backup.hour": {
        "category": "備份設定",
        "default": 3,
        "type": "integer",
        "description": "依系統時區執行備份的小時",
        "risk": "請避開大量分析時段",
        "min": 0,
        "max": 23,
        "restart": False,
    },
    "backup.retention": {
        "category": "備份設定",
        "default": 14,
        "type": "integer",
        "description": "保留的自動備份數量",
        "risk": "過低會縮短可回復時間",
        "min": 1,
        "max": 365,
        "restart": False,
    },
}


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_defaults(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key,category,value_json,value_type,requires_restart,updated_at) VALUES (?,?,?,?,?,?)",
                [
                    (
                        key,
                        definition["category"],
                        json.dumps(definition["default"], ensure_ascii=False),
                        definition["type"],
                        int(definition.get("restart", False)),
                        now,
                    )
                    for key, definition in SETTING_DEFINITIONS.items()
                ],
            )
            connection.executemany(
                "UPDATE settings SET category=?,value_type=?,requires_restart=? WHERE key=?",
                [
                    (
                        definition["category"],
                        definition["type"],
                        int(definition.get("restart", False)),
                        key,
                    )
                    for key, definition in SETTING_DEFINITIONS.items()
                ],
            )

    def all(self):
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM settings ORDER BY category,key").fetchall()
        return [
            dict(row)
            | {"value": json.loads(row["value_json"]), "definition": SETTING_DEFINITIONS.get(row["key"], {})}
            for row in rows
        ]

    def get(self, key: str, default=None):
        with self.database.session() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def history(self, limit: int = 100):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT * FROM setting_history ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()

    def update(self, key: str, value, *, changed_by: str, source_ip: str) -> None:
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            raise KeyError(key)
        value_type = definition["type"]
        if value_type == "integer":
            value = int(value)
        elif value_type == "number":
            value = float(value)
        elif value_type == "boolean":
            value = value is True or str(value).lower() in {"1", "true", "on", "yes"}
        else:
            value = str(value)
            if "min_length" in definition and len(value.strip()) < definition["min_length"]:
                raise ValueError(f"{key} 內容不可少於 {definition['min_length']} 個字元")
            if "max_length" in definition and len(value) > definition["max_length"]:
                raise ValueError(f"{key} 內容不可超過 {definition['max_length']} 個字元")
        if "choices" in definition and value not in definition["choices"]:
            choices = "、".join(str(item) for item in definition["choices"])
            raise ValueError(f"{key} 只允許：{choices}")
        if definition.get("pattern") == "time_hhmm":
            parts = str(value).split(":")
            if (
                len(parts) != 2
                or not all(part.isdigit() for part in parts)
                or not 0 <= int(parts[0]) <= 23
                or not 0 <= int(parts[1]) <= 59
                or len(parts[0]) != 2
                or len(parts[1]) != 2
            ):
                raise ValueError(f"{key} 必須使用 00:00 到 23:59 格式")
        if definition.get("pattern") == "optional_http_url" and value:
            parsed = urlparse(str(value))
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(f"{key} 必須是無帳密的完整 http:// 或 https:// URL")
        if key in {"general.timezone", "device.default_timezone"}:
            try:
                ZoneInfo(str(value))
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"{key} 不是有效的 IANA 時區") from exc
        if (
            "min" in definition
            and value < definition["min"]
            or "max" in definition
            and value > definition["max"]
        ):
            raise ValueError(f"{key} 超出合法範圍")
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(value, ensure_ascii=False)
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    "SELECT value_json FROM settings WHERE key=?", (key,)
                ).fetchone()
                old_summary = previous["value_json"] if previous else "未設定"
                connection.execute(
                    "UPDATE settings SET value_json=?,updated_by=?,updated_at=? WHERE key=?",
                    (encoded, changed_by, now, key),
                )
                connection.execute(
                    """
                    INSERT INTO setting_history(key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,requires_restart)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        now,
                        changed_by,
                        old_summary[:500],
                        encoded[:500],
                        source_ip[:64],
                        int(definition.get("restart", False)),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise


class SecretStore:
    def __init__(self, database: Database, master_secret: str) -> None:
        self.database = database
        key = base64.urlsafe_b64encode(sha256(master_secret.encode("utf-8")).digest())
        self.cipher = Fernet(key)

    def set(self, key: str, value: str, updated_by: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        register_secret(value)
        encrypted = self.cipher.encrypt(value.encode("utf-8"))
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO secrets(key,encrypted_value,updated_by,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET encrypted_value=excluded.encrypted_value,updated_by=excluded.updated_by,updated_at=excluded.updated_at
                """,
                (key, encrypted, updated_by, now),
            )

    def get(self, key: str) -> str | None:
        with self.database.session() as connection:
            row = connection.execute("SELECT encrypted_value FROM secrets WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = self.cipher.decrypt(bytes(row["encrypted_value"])).decode("utf-8")
            register_secret(value)
            return value
        except InvalidToken as exc:
            raise RuntimeError("SEC-001 無法解密敏感設定；請確認主密鑰") from exc

    def delete(self, key: str) -> None:
        with self.database.session() as connection:
            connection.execute("DELETE FROM secrets WHERE key=?", (key,))
