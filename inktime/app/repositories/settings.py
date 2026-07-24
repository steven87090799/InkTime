from __future__ import annotations

from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
import math
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken

from inktime import __version__
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
    "observability.debug_enabled": {"category": "系統監控與除錯", "default": False, "type": "boolean", "description": "Debug 事件記錄；到期自動關閉", "risk": "只用於短期診斷，避免增加 SQLite 寫入", "restart": False},
    "observability.debug_level": {"category": "系統監控與除錯", "default": "normal", "type": "string", "description": "Debug 詳細程度", "risk": "detailed 只限短期使用", "choices": ["normal", "detailed"], "restart": False},
    "observability.debug_components": {"category": "系統監控與除錯", "default": "", "type": "string", "description": "Debug 元件，每行一項；留空為全部", "risk": "不記錄密鑰、Token、未遮蔽路徑或 GPS", "multiline": True, "rows": 4, "max_length": 2000, "restart": False},
    "observability.debug_auto_disable_minutes": {"category": "系統監控與除錯", "default": 60, "type": "integer", "description": "Debug 自動關閉分鐘數", "risk": "到期後僅保留 WARNING 以上與重要工作狀態", "min": 1, "max": 1440, "restart": False},
    "observability.activity_retention_days": {"category": "系統監控與除錯", "default": 14, "type": "integer", "description": "WARNING 以上與重要事件保留天數", "risk": "DEBUG 採較短保留，避免資料庫膨脹", "min": 1, "max": 365, "restart": False},
    "observability.debug_retention_hours": {"category": "系統監控與除錯", "default": 24, "type": "integer", "description": "DEBUG 事件保留小時", "risk": "短期診斷完成後自動清理", "min": 1, "max": 168, "restart": False},
    "observability.activity_max_rows": {"category": "系統監控與除錯", "default": 50000, "type": "integer", "description": "Activity 最大列數", "risk": "超過時優先清理最舊 DEBUG/INFO", "min": 1000, "max": 500000, "restart": False},
    "observability.activity_poll_seconds": {"category": "系統監控與除錯", "default": 5, "type": "integer", "description": "監控頁有界輪詢秒數", "risk": "最小 3 秒以避免 NAS 待機寫入或 CPU 壓力", "min": 3, "max": 60, "restart": False},
    "observability.stuck_job_minutes": {"category": "系統監控與除錯", "default": 5, "type": "integer", "description": "工作 heartbeat 過期告警門檻", "risk": "過低可能誤判長時間模型請求", "min": 1, "max": 120, "restart": False},
    "analysis.strategy": {
        "category": "分析設定",
        "default": "smart_two_stage",
        "type": "string",
        "description": "新工作的預設分析策略",
        "risk": "高品質策略成本較高",
        "choices": ["local", "low_cost", "smart_two_stage", "high_quality"],
        "restart": False,
    },
    "analysis.advanced_caption_enabled": {
        "category": "照片描述與相框文案",
        "default": False,
        "type": "boolean",
        "description": "進階照片描述與相框文案；預設關閉，關閉時完全使用既有 Prompt、Schema 與快取行為",
        "risk": "啟用後的新分析會使用文案設定產生不同快取；不會改寫舊分析",
        "restart": False,
    },
    "analysis.caption_variants_enabled": {
        "category": "照片描述與相框文案",
        "default": False,
        "type": "boolean",
        "description": "同一次高品質圖片分析產生五種相框候選；需先啟用進階文案功能",
        "risk": "候選只保存在既有 semantic_json，切換顯示風格不會再次上傳圖片或呼叫模型",
        "restart": False,
    },
    "analysis.caption_min_chars": {
        "category": "照片描述與相框文案", "default": 120, "type": "integer",
        "description": "詳細照片描述最少字元數", "risk": "必須與目標、上限保持 min ≤ target ≤ max",
        "min": 0, "max": 1000, "restart": False,
    },
    "analysis.caption_target_chars": {
        "category": "照片描述與相框文案", "default": 160, "type": "integer",
        "description": "詳細照片描述的大致目標字元數，不要求模型精確湊字數", "risk": "必須與最少、上限保持 min ≤ target ≤ max",
        "min": 0, "max": 1000, "restart": False,
    },
    "analysis.caption_max_chars": {
        "category": "照片描述與相框文案", "default": 220, "type": "integer",
        "description": "詳細照片描述最多字元數", "risk": "必須與最少、目標保持 min ≤ target ≤ max",
        "min": 0, "max": 1000, "restart": False,
    },
    "analysis.side_caption_min_chars": {
        "category": "照片描述與相框文案", "default": 10, "type": "integer",
        "description": "相框一句話最少字元數", "risk": "必須與目標、上限保持 min ≤ target ≤ max",
        "min": 0, "max": 120, "restart": False,
    },
    "analysis.side_caption_target_chars": {
        "category": "照片描述與相框文案", "default": 22, "type": "integer",
        "description": "相框一句話的大致目標字元數", "risk": "必須與最少、上限保持 min ≤ target ≤ max",
        "min": 0, "max": 120, "restart": False,
    },
    "analysis.side_caption_max_chars": {
        "category": "照片描述與相框文案", "default": 42, "type": "integer",
        "description": "相框一句話最多字元數", "risk": "必須與最少、目標保持 min ≤ target ≤ max",
        "min": 0, "max": 120, "restart": False,
    },
    "analysis.copy_default_style": {
        "category": "照片描述與相框文案", "default": "natural", "type": "string",
        "description": "相框預設顯示的已儲存候選風格", "risk": "只切換既有候選，不會重新分析圖片",
        "choices": ["natural", "warm", "literary", "humorous", "minimal"], "restart": False,
    },
    "analysis.copy_humor_level": {
        "category": "照片描述與相框文案", "default": 1, "type": "integer",
        "description": "相框文案幽默程度（0 為不刻意幽默）", "risk": "過高可能降低正式場合的適用性",
        "min": 0, "max": 5, "restart": False,
    },
    "analysis.copy_poetic_level": {
        "category": "照片描述與相框文案", "default": 1, "type": "integer",
        "description": "相框文案詩意程度（0 為最直白）", "risk": "過高可能讓文案較含蓄",
        "min": 0, "max": 5, "restart": False,
    },
    "analysis.copy_avoid_cliche": {
        "category": "照片描述與相框文案", "default": True, "type": "boolean",
        "description": "避免雞湯、濫情、空泛與模板句", "risk": "會限制模型可使用的常見文案語氣", "restart": False,
    },
    "analysis.copy_avoid_direct_description": {
        "category": "照片描述與相框文案", "default": True, "type": "boolean",
        "description": "相框一句話避免只是直接重述照片內容", "risk": "仍以照片可確認內容為界，不可虛構故事", "restart": False,
    },
    "analysis.copy_forbid_exclamation": {
        "category": "照片描述與相框文案", "default": True, "type": "boolean",
        "description": "相框一句話不使用驚嘆號", "risk": "降低強烈語氣", "restart": False,
    },
    "analysis.copy_forbid_like_phrase": {
        "category": "照片描述與相框文案", "default": True, "type": "boolean",
        "description": "避免使用像是、彷彿、彷佛等比喻起手式", "risk": "限制部分文學修辭", "restart": False,
    },
    "analysis.copy_max_commas": {
        "category": "照片描述與相框文案", "default": 2, "type": "integer",
        "description": "相框一句話最多逗號數", "risk": "過低會讓長句較不易閱讀",
        "min": 0, "max": 10, "restart": False,
    },
    "analysis.copy_avoid_abstract_ending": {
        "category": "照片描述與相框文案", "default": True, "type": "boolean",
        "description": "避免以空泛人生結論收尾", "risk": "限制總結式文案", "restart": False,
    },
    "analysis.copy_banned_words": {
        "category": "照片描述與相框文案", "default": "世界\n時光\n歲月\n治癒\n剛剛好\n悄悄\n慢慢\n值得珍藏\n美好瞬間\n時光定格\n歲月靜好\n生活中的小確幸\n一切都是最好的安排", "type": "string",
        "description": "每行一個禁止詞；只在進階文案啟用時套用", "risk": "過多禁止詞可能使文案選詞受限",
        "multiline": True, "rows": 8, "max_length": 4000, "restart": False,
    },
    "analysis.copy_banned_patterns": {
        "category": "照片描述與相框文案", "default": "", "type": "string",
        "description": "每行一個禁止句型；只在進階文案啟用時套用", "risk": "請使用可理解的文字片段，非正規表示式",
        "multiline": True, "rows": 5, "max_length": 4000, "restart": False,
    },
    "analysis.copy_custom_rules": {
        "category": "照片描述與相框文案", "default": "", "type": "string",
        "description": "額外文案規則；只在進階文案啟用時傳給模型", "risk": "不可要求模型猜測人物關係、地點或事件",
        "multiline": True, "rows": 6, "max_length": 8000, "restart": False,
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
    "render.caption_wrap_enabled": {
        "category": "照片描述與相框文案",
        "default": False,
        "type": "boolean",
        "description": "相框文案依實際字型像素寬度換行；預設關閉並維持既有單行截斷",
        "risk": "啟用後最多兩行，必要時縮小字型；完整原文不會被修改",
        "restart": False,
    },
    "render.caption_max_lines": {
        "category": "照片描述與相框文案", "default": 2, "type": "integer",
        "description": "相框文案開啟換行時的最大行數", "risk": "Footer 空間不足時仍會縮小或截斷",
        "min": 1, "max": 2, "restart": False,
    },
    "render.caption_min_font_size": {
        "category": "照片描述與相框文案", "default": 17, "type": "integer",
        "description": "相框文案開啟換行時可縮小到的最小字型大小", "risk": "字型過小會影響電子紙可讀性",
        "min": 10, "max": 24, "restart": False,
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


SETTINGS_SCHEMA_VERSION = 1
SETTINGS_SNAPSHOT_LIMIT = 100
RANKING_WEIGHT_KEYS = (
    "analysis.ranking_memory_weight",
    "analysis.ranking_beauty_weight",
    "analysis.ranking_technical_weight",
    "analysis.ranking_emotion_weight",
)
PRIVATE_LOCATION_KEYS = {
    "home_latitude",
    "home_longitude",
    "render.weather_latitude",
    "render.weather_longitude",
}
SENSITIVE_STATUS_KEYS = PRIVATE_LOCATION_KEYS | {
    "render.font_path",
    "notification.webhook_url",
}
RUNTIME_UNWIRED_KEYS = {
    "observability.debug_level",
    "observability.debug_components",
    "observability.activity_poll_seconds",
    "budget.daily_warning",
    "budget.monthly_warning",
    "budget.job_default",
    "device.legacy_api_enabled",
}
DEVICE_OVERRIDE_KEYS = {
    "render.layout",
    "render.frame_orientation",
    "render.fit_mode",
    "render.profile",
    "device.default_timezone",
    "device.default_schedule",
    "device.default_rotation",
    "device.default_panel_profile",
}
_BASIC_KEYS = {
    "general.timezone",
    "analysis.strategy",
    "analysis.advanced_caption_enabled",
    "analysis.caption_variants_enabled",
    "analysis.ai_mode",
    "analysis.ai_top_n",
    "analysis.ai_daily_photo_limit",
    "analysis.ai_monthly_photo_limit",
    "analysis.prefilter_enabled",
    "analysis.prefilter_sensitivity",
    "render.layout",
    "render.frame_orientation",
    "render.fit_mode",
    "render.caption_wrap_enabled",
    "render.show_capture_date",
    "render.show_location",
    "render.profile",
    "render.dither",
    "device.default_schedule",
    "notification.device_offline_enabled",
    "backup.schedule_enabled",
    "backup.retention",
}
_HIGH_RISK_KEYS = {
    "analysis.ai_mode",
    "analysis.ai_daily_photo_limit",
    "analysis.ai_monthly_photo_limit",
    "analysis.concurrency",
    "analysis.max_retries",
    "model.low_model",
    "model.high_model",
    "budget.daily_warning",
    "budget.daily_stop",
    "budget.monthly_warning",
    "budget.monthly_stop",
    "budget.job_default",
    "budget.photo_max",
    "budget.max_tokens",
    "security.session_minutes",
    "notification.webhook_url",
    "render.font_path",
    "device.legacy_api_enabled",
} | PRIVATE_LOCATION_KEYS
_LABEL_OVERRIDES = {
    "general.timezone": "系統時區",
    "analysis.strategy": "新工作分析策略",
    "analysis.ai_mode": "AI 分析模式",
    "analysis.ai_top_n": "AI 候選照片上限",
    "analysis.ai_daily_photo_limit": "每日 AI 分析照片上限",
    "analysis.ai_monthly_photo_limit": "每月 AI 分析照片上限",
    "analysis.advanced_caption_enabled": "進階照片描述與相框文案",
    "analysis.caption_variants_enabled": "相框文案候選版本",
    "analysis.caption_min_chars": "詳細描述最少字數",
    "analysis.caption_target_chars": "詳細描述目標字數",
    "analysis.caption_max_chars": "詳細描述最多字數",
    "analysis.side_caption_min_chars": "相框短文最少字數",
    "analysis.side_caption_target_chars": "相框短文目標字數",
    "analysis.side_caption_max_chars": "相框短文最多字數",
    "render.caption_wrap_enabled": "Footer 多行文案",
    "render.caption_max_lines": "Footer 最多行數",
    "render.caption_min_font_size": "Footer 最小字體",
    "observability.debug_enabled": "暫時啟用 Debug 記錄",
    "observability.activity_retention_days": "重要 Activity 保留天數",
    "observability.activity_max_rows": "Activity 最大列數",
    "observability.stuck_job_minutes": "工作卡住判定時間",
    "analysis.concurrency": "AI 分析並行數",
    "analysis.max_retries": "AI 分析重試次數",
    "render.layout": "相框版型",
    "render.frame_orientation": "相框方向",
    "render.fit_mode": "照片填入方式",
    "render.profile": "電子紙顯示 Profile",
    "render.dither": "抖色演算法",
    "render.dither_strength": "抖色強度",
    "render.color_distance": "色差計算方式",
    "security.session_minutes": "登入工作階段時間",
}


def _risk_level(description: str, key: str) -> str:
    if key in _HIGH_RISK_KEYS:
        return "high"
    text = f"{key} {description}"
    if any(
        marker in text
        for marker in (
            "敏感",
            "完整照片庫",
            "密鑰",
            "Token",
            "並行",
            "重試",
            "刪除",
            "安全",
            "成本",
            "預算",
            "legacy",
        )
    ):
        return "high"
    if any(
        marker in text
        for marker in ("模型", "分析", "保留", "快取", "通知", "渲染", "排程", "裝置")
    ):
        return "medium"
    return "low"


def _effective_scope(key: str, definition: dict[str, Any]) -> str:
    if key in RUNTIME_UNWIRED_KEYS:
        return "not_wired"
    if definition.get("restart"):
        return "restart"
    if key.startswith(("analysis.", "model.", "budget.", "scanner.", "worker.", "scheduler.")):
        return "next_job"
    if key.startswith("render."):
        return "next_render"
    if key.startswith("device.default_"):
        return "future_device_only"
    return "dynamic"


def _metadata_dependencies(key: str) -> list[dict[str, Any]]:
    if key == "analysis.caption_variants_enabled":
        return [{"key": "analysis.advanced_caption_enabled", "equals": True}]
    if key.startswith("analysis.copy_") or key.startswith("analysis.caption_") or key.startswith(
        "analysis.side_caption_"
    ):
        return [{"key": "analysis.advanced_caption_enabled", "equals": True}]
    if key in {"render.caption_max_lines", "render.caption_min_font_size"}:
        return [{"key": "render.caption_wrap_enabled", "equals": True}]
    if key.startswith(("model.", "budget.")) or key in {
        "analysis.ai_top_n",
        "analysis.ai_daily_photo_limit",
        "analysis.ai_monthly_photo_limit",
        "analysis.stage_two_threshold",
        "analysis.max_retries",
        "analysis.concurrency",
    }:
        return [{"key": "analysis.ai_mode", "not_equals": "off"}]
    return []


def _validation_group(key: str) -> str | None:
    if key.startswith(("analysis.caption_", "analysis.side_caption_")):
        return "caption_ranges"
    if key in RANKING_WEIGHT_KEYS:
        return "ranking_weights"
    if key.startswith("budget."):
        return "budget_limits"
    if key.startswith("observability."):
        return "activity_retention"
    return None


def _govern_definition(key: str, definition: dict[str, Any]) -> None:
    risk_description = str(definition.get("risk", "依安全範圍調整"))
    risk = _risk_level(risk_description, key)
    cache_impact = key.startswith(("analysis.caption_", "analysis.copy_", "model.")) or key in {
        "analysis.advanced_caption_enabled",
        "analysis.caption_variants_enabled",
    }
    definition.update(
        {
            "label_zh_tw": _LABEL_OVERRIDES.get(
                key, str(definition.get("description", key)).split("；", 1)[0]
            ),
            "risk": risk,
            "risk_description": risk_description,
            "safe_fallback": definition["default"],
            "visibility": "sensitive" if key in SENSITIVE_STATUS_KEYS else "public",
            "advanced": key not in _BASIC_KEYS or risk == "high",
            "secret": False,
            "restart_required": bool(definition.get("restart", False)),
            "effective_scope": _effective_scope(key, definition),
            "cache_impact": cache_impact,
            "reanalysis_impact": cache_impact or key.startswith(
                ("analysis.prefilter_", "analysis.e6_", "analysis.scoring_")
            ),
            "rerender_impact": key.startswith("render."),
            "device_override_allowed": key in DEVICE_OVERRIDE_KEYS,
            "dependencies": _metadata_dependencies(key),
            "conflicts": [],
            "validation_group": _validation_group(key),
            "runtime_wired": key not in RUNTIME_UNWIRED_KEYS,
            "snapshot_allowed": key not in SENSITIVE_STATUS_KEYS,
            "export_allowed": key not in SENSITIVE_STATUS_KEYS,
            "existing_release_unchanged": key.startswith("render."),
            "effective_note": (
                "只影響下一次渲染；既有 Release 不會改變，必須建立新 Release"
                if key.startswith("render.")
                else "只套用到之後新增的裝置"
                if key.startswith("device.default_")
                else "已儲存但尚未生效"
                if key in RUNTIME_UNWIRED_KEYS
                else None
            ),
        }
    )


for _setting_key, _setting_definition in SETTING_DEFINITIONS.items():
    _govern_definition(_setting_key, _setting_definition)


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
                        json.dumps(
                            definition["default"], ensure_ascii=False, allow_nan=False
                        ),
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

    def all(self, *, redact_sensitive: bool = False):
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM settings ORDER BY category,key").fetchall()
        result = []
        for row in rows:
            definition = SETTING_DEFINITIONS.get(row["key"], {})
            public_definition = (
                self.public_metadata(str(row["key"]))
                if redact_sensitive and row["key"] in SENSITIVE_STATUS_KEYS
                else definition
            )
            value = json.loads(row["value_json"])
            source = "System" if row["updated_by"] else "Default"
            public_value = (
                self._status_value(value)
                if redact_sensitive and row["key"] in SENSITIVE_STATUS_KEYS
                else value
            )
            runtime_wired = bool(definition.get("runtime_wired", True))
            result.append(
                dict(row)
                | {
                    "value_json": (
                        json.dumps(
                            public_value,
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                        if redact_sensitive and row["key"] in SENSITIVE_STATUS_KEYS
                        else row["value_json"]
                    ),
                    "value": public_value,
                    "stored_value": public_value if source == "System" else None,
                    "effective_value": public_value if runtime_wired else None,
                    "effective_source": source if runtime_wired else "NotWired",
                    "definition": public_definition,
                }
            )
        return result

    def get(self, key: str, default=None):
        with self.database.session() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def history(self, limit: int = 100, *, redact_source_ip: bool = False):
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM setting_history ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        if not redact_source_ip:
            return rows
        return [dict(row) | {"source_ip": "已遮蔽"} for row in rows]

    def snapshots(self, limit: int = 50):
        with self.database.session() as connection:
            rows = connection.execute(
                    """
                    SELECT id,created_at,actor_id,reason,changed_keys_json,
                           schema_version,application_version,rollback_source_snapshot_id
                    FROM settings_snapshots ORDER BY created_at DESC,id DESC LIMIT ?
                    """,
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
        return [
            dict(row)
            | {"changed_keys_count": len(json.loads(row["changed_keys_json"]))}
            for row in rows
        ]

    def _snapshot_record(self, snapshot_id: str) -> dict[str, Any]:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM settings_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            items = connection.execute(
                """
                SELECT key,old_value_json,new_value_json,restored_default
                FROM settings_snapshot_items WHERE snapshot_id=? ORDER BY key
                """,
                (snapshot_id,),
            ).fetchall()
        result = dict(row)
        result["changed_keys"] = json.loads(result.pop("changed_keys_json"))
        result["before"] = json.loads(result.pop("before_json"))
        result["after"] = json.loads(result.pop("after_json"))
        result["items"] = [
            {
                "key": item["key"],
                "old_value": json.loads(item["old_value_json"]),
                "new_value": json.loads(item["new_value_json"]),
                "restored_default": bool(item["restored_default"]),
            }
            for item in items
        ]
        item_keys = {str(item["key"]) for item in result["items"]}
        result["items"].extend(
            {
                "key": key,
                "old_value": result["before"].get(key),
                "new_value": result["after"].get(key),
                "restored_default": False,
            }
            for key in result["changed_keys"]
            if key not in item_keys
        )
        result["items"].sort(key=lambda item: str(item["key"]))
        return result

    def snapshot(self, snapshot_id: str) -> dict[str, Any]:
        result = self._snapshot_record(snapshot_id)
        result.pop("source_ip", None)
        result["before"] = {
            key: value
            for key, value in result["before"].items()
            if key in SETTING_DEFINITIONS and key not in SENSITIVE_STATUS_KEYS
        }
        result["after"] = {
            key: value
            for key, value in result["after"].items()
            if key in SETTING_DEFINITIONS and key not in SENSITIVE_STATUS_KEYS
        }
        result["items"] = [
            {
                **item,
                "old_value": self._public_snapshot_value(
                    str(item["key"]), item["old_value"]
                ),
                "new_value": self._public_snapshot_value(
                    str(item["key"]), item["new_value"], changed=True
                ),
                "metadata": self.public_metadata(str(item["key"])),
            }
            for item in result["items"]
        ]
        return result

    @staticmethod
    def public_metadata(key: str) -> dict[str, Any]:
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            return {
                "key": key,
                "label_zh_tw": "已移除設定",
                "category": "已移除設定",
                "description": "此設定已從目前版本移除，僅保留歷史紀錄",
                "risk": "high",
                "risk_description": "Rollback 會安全跳過此設定",
                "type": "removed",
                "default": None,
                "min": None,
                "max": None,
                "choices": None,
                "choice_labels": None,
                "safe_fallback": None,
                "visibility": "removed",
                "advanced": True,
                "secret": False,
                "restart_required": False,
                "effective_scope": "not_wired",
                "cache_impact": False,
                "reanalysis_impact": False,
                "rerender_impact": False,
                "device_override_allowed": False,
                "dependencies": [],
                "conflicts": [],
                "validation_group": None,
                "runtime_wired": False,
                "existing_release_unchanged": True,
                "effective_note": "已移除設定；Rollback 會安全跳過",
                "removed": True,
            }
        fields = (
            "label_zh_tw",
            "category",
            "description",
            "risk",
            "risk_description",
            "type",
            "default",
            "min",
            "max",
            "choices",
            "choice_labels",
            "safe_fallback",
            "visibility",
            "advanced",
            "secret",
            "restart_required",
            "effective_scope",
            "cache_impact",
            "reanalysis_impact",
            "rerender_impact",
            "device_override_allowed",
            "dependencies",
            "conflicts",
            "validation_group",
            "runtime_wired",
            "existing_release_unchanged",
            "effective_note",
        )
        metadata = {"key": key} | {field: definition.get(field) for field in fields}
        if key in SENSITIVE_STATUS_KEYS:
            metadata["default"] = SettingsRepository._status_value(
                definition.get("default")
            )
            metadata["safe_fallback"] = metadata["default"]
        return metadata

    @staticmethod
    def _status_value(value: Any, *, changed: bool = False) -> dict[str, str]:
        configured = value is not None and value != ""
        if changed:
            return {"status": "已變更" if configured else "已清除"}
        return {"status": "已設定" if configured else "未設定"}

    @staticmethod
    def _public_snapshot_value(
        key: str, value: Any, *, changed: bool = False
    ) -> Any:
        if key not in SETTING_DEFINITIONS:
            return {"status": "已移除設定"}
        if key in SENSITIVE_STATUS_KEYS:
            if (
                isinstance(value, dict)
                and value.get("status")
                in {"未設定", "已設定", "已變更", "已清除"}
            ):
                return {"status": str(value["status"])}
            return SettingsRepository._status_value(value, changed=changed)
        return value

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            raise KeyError(key)
        value_type = definition["type"]
        if value_type == "integer":
            if isinstance(value, bool):
                raise ValueError(f"{key} 必須是整數")
            if isinstance(value, int):
                pass
            elif isinstance(value, float):
                if not math.isfinite(value) or not value.is_integer():
                    raise ValueError(f"{key} 必須是整數")
                value = int(value)
            elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
                value = int(value.strip())
            else:
                raise ValueError(f"{key} 必須是整數")
        elif value_type == "number":
            if isinstance(value, bool):
                raise ValueError(f"{key} 必須是數字")
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必須是數字") from exc
            if not math.isfinite(value):
                raise ValueError(f"{key} 必須是有限數字")
        elif value_type == "boolean":
            if isinstance(value, bool):
                pass
            elif isinstance(value, str) and value.lower() in {"1", "true", "on", "yes"}:
                value = True
            elif isinstance(value, str) and value.lower() in {"0", "false", "off", "no"}:
                value = False
            else:
                raise ValueError(f"{key} 必須是布林值")
        elif not isinstance(value, str):
            raise ValueError(f"{key} 必須是文字")

        if isinstance(value, str):
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
        return value

    @staticmethod
    def _validate_all(values: dict[str, Any]) -> None:
        SettingsRepository._validate_caption_ranges(values)
        total = sum(float(values[key]) for key in RANKING_WEIGHT_KEYS)
        if abs(total - 100.0) > 0.001:
            raise ValueError("四項排序權重合計必須為 100%")
        if float(values["budget.daily_warning"]) > float(values["budget.daily_stop"]):
            raise ValueError("每日預算警告值不可高於停止值")
        if float(values["budget.monthly_warning"]) > float(values["budget.monthly_stop"]):
            raise ValueError("每月預算警告值不可高於停止值")
        if int(values["observability.activity_retention_days"]) < 7:
            raise ValueError("重要 Activity 至少保留 7 天，以保護錯誤追蹤與安全回復")
        if int(values["observability.activity_max_rows"]) < 1000:
            raise ValueError("Activity 最大列數不得低於 1000")

    @staticmethod
    def _values_from_connection(connection) -> dict[str, Any]:
        rows = connection.execute("SELECT key,value_json FROM settings").fetchall()
        values = {
            key: definition["default"] for key, definition in SETTING_DEFINITIONS.items()
        }
        values.update({str(row["key"]): json.loads(row["value_json"]) for row in rows})
        return values

    def prepare_updates(
        self, updates: dict[str, Any], *, reject_control_center: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not isinstance(updates, dict):
            raise ValueError("設定更新必須是 JSON 物件")
        normalized: dict[str, Any] = {}
        for raw_key, value in updates.items():
            key = str(raw_key)
            definition = SETTING_DEFINITIONS.get(key)
            if definition is None:
                raise KeyError(key)
            if reject_control_center and definition.get("control_center"):
                raise PermissionError(key)
            if not definition.get("runtime_wired", True):
                raise PermissionError(f"{key} 尚未接上 Runtime，僅供唯讀")
            normalized[key] = self._coerce(key, value)
        with self.database.session() as connection:
            current = self._values_from_connection(connection)
        merged = current | normalized
        self._validate_all(merged)
        changed = {key: value for key, value in normalized.items() if current.get(key) != value}
        return changed, current, merged

    @staticmethod
    def _validate_caption_ranges(values: dict[str, Any]) -> None:
        for prefix, maximum in (("analysis.caption", 1000), ("analysis.side_caption", 120)):
            minimum = int(values[f"{prefix}_min_chars"])
            target = int(values[f"{prefix}_target_chars"])
            upper = int(values[f"{prefix}_max_chars"])
            if not 0 <= minimum <= target <= upper <= maximum:
                raise ValueError(f"{prefix} 長度必須符合 0 ≤ min ≤ target ≤ max ≤ {maximum}")

    def _caption_range_values(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        keys = (
            "analysis.caption_min_chars", "analysis.caption_target_chars", "analysis.caption_max_chars",
            "analysis.side_caption_min_chars", "analysis.side_caption_target_chars", "analysis.side_caption_max_chars",
        )
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT key,value_json FROM settings WHERE key IN (?,?,?,?,?,?)", keys
            ).fetchall()
        values = {row["key"]: json.loads(row["value_json"]) for row in rows}
        values.update(overrides or {})
        return values

    def validate_caption_updates(self, updates: dict[str, Any]) -> None:
        self.prepare_updates(updates)

    @staticmethod
    def _snapshot_value(key: str, value: Any, *, changed: bool = False) -> Any:
        if not SETTING_DEFINITIONS[key].get("snapshot_allowed", True):
            return SettingsRepository._status_value(value, changed=changed)
        return value

    def _create_snapshot(
        self,
        connection,
        *,
        before: dict[str, Any],
        after: dict[str, Any],
        changed: dict[str, Any],
        actor_id: str,
        source_ip: str,
        reason: str | None,
        rollback_source_snapshot_id: str | None,
    ) -> str:
        snapshot_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        safe_before = {
            key: value
            for key, value in before.items()
            if SETTING_DEFINITIONS[key].get("snapshot_allowed", True)
        }
        safe_after = {
            key: value
            for key, value in after.items()
            if SETTING_DEFINITIONS[key].get("snapshot_allowed", True)
        }
        changed_keys = sorted(changed)
        connection.execute(
            """
            INSERT INTO settings_snapshots(
                id,created_at,actor_id,source_ip,reason,before_json,after_json,changed_keys_json,
                schema_version,application_version,rollback_source_snapshot_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                now,
                actor_id,
                source_ip[:64],
                (reason or "").strip()[:500] or None,
                json.dumps(
                    safe_before,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                ),
                json.dumps(
                    safe_after,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                ),
                json.dumps(changed_keys, ensure_ascii=False, allow_nan=False),
                SETTINGS_SCHEMA_VERSION,
                __version__,
                rollback_source_snapshot_id,
            ),
        )
        connection.executemany(
            """
            INSERT INTO settings_snapshot_items(
                snapshot_id,key,old_value_json,new_value_json,restored_default
            ) VALUES (?,?,?,?,?)
            """,
            [
                (
                    snapshot_id,
                    key,
                    json.dumps(
                        self._snapshot_value(key, before[key]),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    json.dumps(
                        self._snapshot_value(key, after[key], changed=True),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    int(after[key] == SETTING_DEFINITIONS[key]["default"]),
                )
                for key in changed_keys
            ],
        )
        snapshot_rows = connection.execute(
            """
            SELECT id,rollback_source_snapshot_id
            FROM settings_snapshots ORDER BY created_at DESC,id DESC
            """
        ).fetchall()
        if len(snapshot_rows) > SETTINGS_SNAPSHOT_LIMIT:
            latest_rollback = next(
                (
                    row
                    for row in snapshot_rows
                    if row["rollback_source_snapshot_id"] is not None
                ),
                None,
            )
            protected = set()
            if latest_rollback is not None:
                protected.update(
                    {
                        str(latest_rollback["id"]),
                        str(latest_rollback["rollback_source_snapshot_id"]),
                    }
                )
            keep = set(protected)
            for row in snapshot_rows:
                if len(keep) >= SETTINGS_SNAPSHOT_LIMIT:
                    break
                keep.add(str(row["id"]))
            removable = [
                str(row["id"]) for row in snapshot_rows if str(row["id"]) not in keep
            ]
            connection.executemany(
                """
                UPDATE settings_snapshots SET rollback_source_snapshot_id=NULL
                WHERE rollback_source_snapshot_id=?
                """,
                [(snapshot_id,) for snapshot_id in removable],
            )
            connection.executemany(
                "DELETE FROM settings_snapshots WHERE id=?",
                [(snapshot_id,) for snapshot_id in removable],
            )
        return snapshot_id

    def update_many(
        self,
        updates: dict[str, Any],
        *,
        changed_by: str,
        source_ip: str,
        reason: str | None = None,
        rollback_source_snapshot_id: str | None = None,
        reject_control_center: bool = False,
    ) -> dict[str, Any]:
        changed, _current, _merged = self.prepare_updates(
            updates, reject_control_center=reject_control_center
        )
        if not changed:
            return {"updated": 0, "changed_keys": [], "snapshot_id": None}
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            before = self._values_from_connection(connection)
            normalized = {key: self._coerce(key, value) for key, value in changed.items()}
            after = before | normalized
            self._validate_all(after)
            actual = {
                key: value for key, value in normalized.items() if before.get(key) != value
            }
            if not actual:
                return {"updated": 0, "changed_keys": [], "snapshot_id": None}
            snapshot_id = self._create_snapshot(
                connection,
                before=before,
                after=after,
                changed=actual,
                actor_id=changed_by,
                source_ip=source_ip,
                reason=reason,
                rollback_source_snapshot_id=rollback_source_snapshot_id,
            )
            for key, value in actual.items():
                definition = SETTING_DEFINITIONS[key]
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                previous_summary = json.dumps(
                    self._snapshot_value(key, before[key]),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                new_summary = json.dumps(
                    self._snapshot_value(key, value, changed=True),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                connection.execute(
                    "UPDATE settings SET value_json=?,updated_by=?,updated_at=? WHERE key=?",
                    (
                        encoded,
                        None if value == definition["default"] else changed_by,
                        now,
                        key,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO setting_history(
                        key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,
                        requires_restart
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        now,
                        changed_by,
                        previous_summary[:500],
                        new_summary[:500],
                        source_ip[:64],
                        int(definition.get("restart_required", False)),
                    ),
                )
        return {
            "updated": len(actual),
            "changed_keys": sorted(actual),
            "snapshot_id": snapshot_id,
        }

    def rollback_preview(self, snapshot_id: str) -> dict[str, Any]:
        target = self._snapshot_record(snapshot_id)
        target_values = target["before"]
        target_after = target["after"]
        snapshot_changed_keys = list(dict.fromkeys(map(str, target["changed_keys"])))
        recorded_keys = (
            set(snapshot_changed_keys)
            | set(map(str, target_values))
            | set(map(str, target_after))
            | {str(item["key"]) for item in target["items"]}
        )
        with self.database.session() as connection:
            current = self._values_from_connection(connection)
        unknown_keys = sorted(
            key for key in recorded_keys if key not in SETTING_DEFINITIONS
        )
        sensitive_unrestorable_keys = sorted(
            key
            for key in snapshot_changed_keys
            if key in SETTING_DEFINITIONS
            and not SETTING_DEFINITIONS[key].get("snapshot_allowed", True)
        )
        unsupported_keys = sorted(
            key
            for key in snapshot_changed_keys
            if key in SETTING_DEFINITIONS
            and (
                SETTING_DEFINITIONS[key].get("control_center")
                or not SETTING_DEFINITIONS[key].get("runtime_wired", True)
            )
        )
        updates = {
            key: value
            for key, value in target_values.items()
            if key in snapshot_changed_keys
            and key in SETTING_DEFINITIONS
            and key not in unsupported_keys
            and key not in sensitive_unrestorable_keys
            and current.get(key) != value
        }
        changed, _before, merged = self.prepare_updates(updates)
        diff = [
            {
                "key": key,
                "label_zh_tw": SETTING_DEFINITIONS[key]["label_zh_tw"],
                "current_value": current[key],
                "target_value": changed[key],
                "changed_since_snapshot": (
                    key in target_after and current[key] != target_after[key]
                ),
            }
            for key in sorted(changed)
        ]
        overwrites_later_changes = any(
            item["changed_since_snapshot"] for item in diff
        )
        return {
            "snapshot_id": snapshot_id,
            "changed_keys": sorted(changed),
            "unknown_keys": unknown_keys,
            "unsupported_keys": unsupported_keys,
            "sensitive_unrestorable_keys": sensitive_unrestorable_keys,
            "updates": changed,
            "diff": diff,
            "rollback_scope": "snapshot_changed_keys_only",
            "overwrites_changes_after_snapshot": overwrites_later_changes,
            "rollback_notice": (
                "只回復此 Snapshot 的 changed_keys。標示為 Snapshot 後又變更的項目，"
                "若繼續 Rollback，會以目標值覆蓋目前值。"
            ),
            "valid": True,
            "effective_values": {key: merged[key] for key in changed},
        }

    def rollback(
        self,
        snapshot_id: str,
        *,
        changed_by: str,
        source_ip: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        preview = self.rollback_preview(snapshot_id)
        return self.update_many(
            preview["updates"],
            changed_by=changed_by,
            source_ip=source_ip,
            reason=reason or f"Rollback 至 Snapshot {snapshot_id}",
            rollback_source_snapshot_id=snapshot_id,
        )

    def update(
        self,
        key: str,
        value,
        *,
        changed_by: str,
        source_ip: str,
        _caption_ranges_checked: bool = False,
    ) -> None:
        del _caption_ranges_checked
        self.update_many(
            {key: value}, changed_by=changed_by, source_ip=source_ip
        )


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
