# 照片库路径（你自己的相册目录）
IMAGE_DIR = "./test"

# 数据库路径（建议保持默认）
DB_PATH = "./photos.db"

# VLM 渠道列表（按优先级从高到低排列）
# 当某个渠道返回 429 时，自动尝试下一个渠道
API_CHANNELS = [
    {
        "api_url":    "http://127.0.0.1:1234/v1/chat/completions",
        "api_key":    "",
        "model_name": "qwen3-vl-32b-instruct",
    },
    # 可以添加更多渠道，例如：
    # {
    #     "api_url":    "https://other-provider.com/v1/chat/completions",
    #     "api_key":    "sk-xxxxxxxx",
    #     "model_name": "qwen-vl-plus",
    # },
]

# 每次最多处理多少张的图片
BATCH_LIMIT = None

# 请求超时时间（秒）
TIMEOUT = 600

# 某个渠道失败后，临时降低其优先级的冷却时间（秒）
# 例如 A 失败、B 成功后，在冷却期内后续照片会优先从 B 开始请求
CHANNEL_FAILOVER_COOLDOWN_SEC = 300

# 为防止照片隐私泄露，建议为 ESP32 下载路径加一个随机前缀作为密钥
# 前缀修改后，请同步修改 esp32/ink-display-7C-photo/ink-display-7C-photo.ino 固件中的 DAILY_PHOTO_PATH_PREFIX 字段）
DOWNLOAD_KEY = "yourdownloadkey"

# Flask 静态服务
FLASK_HOST = "0.0.0.0"  # noqa: S104 - explicit container/LAN example binding.
FLASK_PORT = 8765
# 此舊設定已停用。Legacy Review／Simulator 只能以部署環境變數
# INKTIME_ENABLE_LEGACY_WEBUI=true 明確開啟；所有環境預設關閉。
ENABLE_REVIEW_WEBUI = False

# 离线中文城市名索引，使用 geonames 数据制作
WORLD_CITIES_CSV = "./data/world_cities_zh.csv"

# 网格大小（纬度/经度度数）；越大越快但精度略差。1.0 对大多数场景够用。
CITY_GRID_DEG = 1.0

# 你的“常驻常驻”坐标（用于判断是否为旅行期间照片，从而对评分进行小幅加成）
# 照片 GPS 距离常驻地超过 HOME_RADIUS_KM，则视为“异地”
# 默认值给了深圳市中心附近（不改也能保持原行为的大致效果）
HOME_LAT = 22.543096
HOME_LON = 114.057865
HOME_RADIUS_KM = 60.0

# 最大接受距离（公里），超出则认为“不在任何城市附近”
CITY_MAX_DISTANCE_KM = 100.0

# 墨水屏渲染 BIN 文件输出目录
BIN_OUTPUT_DIR = "./output"

# 自定义字体路径（为空则退回默认字体）
FONT_PATH = ""

# 每日选片“精彩度”阈值
MEMORY_THRESHOLD = 70.0

# 每日挑选的照片数量
DAILY_PHOTO_QUANTITY = 5
