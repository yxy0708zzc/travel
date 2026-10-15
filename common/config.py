"""
全局配置：.env 读取 + 运行参数常量（不持有任何数据流路径——路径全部由
各 step 的命令行参数指定，经 database.configure_paths() 注入）。
"""

import os
from datetime import datetime, timedelta

# 项目根（.env 默认查找位置；可用各 step 的 --env 覆盖）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str = None) -> dict:
    """读取 .env：KEY=VALUE，# 注释（裸值中 " #" 起视为行内注释），
    引号包裹的值内部可含 #。CLI 单次进程，启动加载一次即可。"""
    path = path or os.path.join(PROJECT_ROOT, ".env")
    env = {}
    if not os.path.exists(path):
        return env
    for line in open(path, "r", encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if v[:1] in ('"', "'"):                  # 引号包裹：取引号内内容（内部 # 不算注释）
            q = v[0]
            end = v.find(q, 1)
            if end != -1:
                v = v[1:end]
        elif v.startswith("#"):
            v = ""
        else:                                    # 裸值：剥 " #" 之后的行内注释
            for sep in (" #", "\t#"):
                i = v.find(sep)
                if i != -1:
                    v = v[:i].rstrip()
        env[k.strip()] = v.strip()
    return env


ENV = load_env()


def env_get(*keys: str, default: str = "") -> str:
    """按优先级取第一个非空环境值，如 env_get("NL_MODEL", "DEFAULT_MODEL")"""
    for k in keys:
        v = (ENV.get(k) or "").strip()
        if v:
            return v
    return default


# ============================================================
# 大模型调用参数（改这里即可，无需动 .env）
# 不同平台 temperature 范围不同（MiMo [0,1.5]、DeepSeek [0,2]）
# ============================================================
NL_TEMPERATURE = 1.4      # 自然语言化温度（提高多样性）
TEST_TEMPERATURE = 0.7    # 测试对话温度
TEST_CHAT_TIMEOUT = 600   # 单轮 LLM 请求超时（秒）。慢推理模型单轮可能极长，
                          # 太短会把对话拦腰掐断（历史事故口径，勿随意调小）

# ============================================================
# 12306 爬虫配置（限速 / 超时 / 重试 / 基准日期）
# ============================================================
CRAWLER_CONFIG = {
    "min_interval": 0.15,                # 请求最小间隔（秒）
    "request_timeout": 15,               # 请求超时（秒）
    "max_retries": 3,                    # 最大重试次数
    "query_date_days_ahead": 3,          # 余票查询基准日期（未来第 N 天）
    "query_date_days_list": [3, 9, 13],  # 票价爬取日期候选，逐天重试
    "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
}

# ============================================================
# 出题器配置
# ============================================================
QUESTION_CONFIG = {
    "max_people_count": 20,     # 需求人数上限
}


def get_query_date() -> str:
    """爬虫基准日期 YYYY-MM-DD"""
    return (datetime.now() + timedelta(days=CRAWLER_CONFIG["query_date_days_ahead"])).strftime("%Y-%m-%d")
