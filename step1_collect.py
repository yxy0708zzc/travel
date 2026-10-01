"""
step1_collect.py —— 数据爬取与处理：12306 余票/票价采集、数据清理与体检

复刻参考项目工作流（语义 1:1）：
- tickets：车站列表(station_name.js) → search API 枚举 G 车次 train_no →
  queryByTrainNo 经停站（时间四路取值、未知站 @自动注册）→ 物化 station_trains →
  同车多号映射 same_trains.json → 体检报告
- prices：相邻站对票价（queryAllPublicPrice；同车多号轮替、3轮×5次×逐天重试、
  高铁/普速席别映射+兜底）→ 非相邻段离线累加；--resume 断点续爬 / --supplement 补缺
- cleanup：同车多号去重（主号=trains 中票价记录最多者）→ 非相邻段补算 →
  删除仍不全车次 → 重建 station_trains；--check 只读体检
- overnight：跨天车（相邻实际 stop_no 配对、时间倒退、双侧 HH:MM 校验）；--apply 才删
- check：只读体检（表计数/缺电报码/缺时间/跨天/票价完整性概览）
- all：tickets → prices → cleanup → overnight → check 串行全流程

所有输入输出路径由参数显式指定；failed_pairs.log 写在 prices-db 同目录。

用法示例：
    python step1_collect.py tickets --railway-db data/railway.db
    python step1_collect.py prices --railway-db data/railway.db --prices-db data/prices.db ^
        [--resume | --supplement] [--train G1] [--workers 3]
    python step1_collect.py cleanup --railway-db data/railway.db --prices-db data/prices.db [--check]
    python step1_collect.py overnight --railway-db data/railway.db --prices-db data/prices.db [--apply]
"""

import argparse
import json
import os
import random
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from common import config
from common import database as db

# ============================================================
# 共享 HTTP 基础：限速器 + 反爬检测 + 带退避重试
# ============================================================

ANTICRAWL_KEYWORDS = [
    "验证码", "captcha", "CAPTCHA", "访问太频繁", "请求太频繁",
    "访问被拒绝", "Access Denied", "Too Many Requests",
    "触发风控", "trigger", "您的请求过于频繁",
]

# 站名归一化：仅保留汉字（12306 站名偶带空格/符号，会导致 name→id 匹配 miss）
_NON_HANZI = re.compile(r"[^\u4e00-\u9fff]")


def _norm_station(name: str) -> str:
    return _NON_HANZI.sub("", name or "")


class RateLimiter:
    """相邻请求间隔 ≥ min_interval（monotonic 时钟，限速防反爬）。
    加锁：prices --workers>1 时多线程共享实例，判定+更新原子化，
    否则并发可同时通过间隔检查，全局请求间隔被击穿、易触发反爬。"""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if self._last > 0 and now - self._last < self.min_interval:
                time.sleep(self.min_interval - (now - self._last))
            self._last = time.monotonic()


def is_anticrawl(resp: Optional[requests.Response]) -> bool:
    """反爬判定：403/429；或 200 但响应体 <1KB 且含关键词（正常页面 ≥1KB）"""
    if resp is None:
        return False
    if resp.status_code in (403, 429):
        return True
    if resp.status_code == 200 and len(resp.text) < 1000:
        body = resp.text.lower()
        return any(kw.lower() in body for kw in ANTICRAWL_KEYWORDS)
    return False


class HttpBase:
    """两个爬虫共用的会话与重试请求。

    backoff 两套退避口径与参考逐秒对齐（参考两个爬虫本就不同）：
    - "collector"（tickets）：反爬 10~20s×次数；非200/异常 0.75s×次数（末次不睡）
    - "price"（prices）：反爬 min(10, 1.5×次)+0.5~2s；非200/异常 min(6,次)+0.3~1.2s（每轮都睡）
    """

    def __init__(self, backoff: str = "collector"):
        cfg = config.CRAWLER_CONFIG
        self.session = requests.Session()
        # 请求头档位与退避口径同源：price 档带参考 price_collector 的完整浏览器仿真头；
        # collector 档与参考 collector 一致仅 UA
        headers = {"User-Agent": cfg["user_agent"]}
        if backoff == "price":
            headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            })
        self.session.headers.update(headers)
        self.rate = RateLimiter(cfg["min_interval"])
        self.max_retries = cfg["max_retries"]
        self.timeout = cfg["request_timeout"]
        self.backoff = backoff

    def get(self, url: str, params: dict = None, headers: dict = None,
            session: requests.Session = None) -> Optional[requests.Response]:
        """带限速与三路退避重试的 GET（反爬/非200/网络异常，口径按 backoff）。全部失败返回 None。"""
        used = session or self.session
        for attempt in range(1, self.max_retries + 1):
            try:
                self.rate.wait()
                hdrs = used.headers.copy()
                if headers:
                    hdrs.update(headers)
                resp = used.get(url, params=params, headers=hdrs, timeout=self.timeout)

                if is_anticrawl(resp):
                    if self.backoff == "collector":
                        print(f"⚠️  ⚠️  被反爬 [尝试 {attempt}/{self.max_retries}] "
                              f"HTTP {resp.status_code}: {url}")
                        if attempt < self.max_retries:
                            time.sleep(random.uniform(10, 20) * attempt)
                    else:
                        print(f"⚠️ 反爬 [尝试 {attempt}/{self.max_retries}] HTTP {resp.status_code}")
                        if attempt < self.max_retries:
                            time.sleep(min(10.0, 1.5 * attempt) + random.uniform(0.5, 2.0))
                    continue
                if resp.status_code != 200:
                    print(f"[重试 {attempt}/{self.max_retries}] HTTP {resp.status_code}: {url}")
                    if self.backoff == "price":
                        time.sleep(min(6.0, 1.0 * attempt) + random.uniform(0.3, 1.2))
                    elif attempt < self.max_retries:
                        time.sleep(config.CRAWLER_CONFIG["min_interval"] * 5 * attempt)
                    continue
                return resp
            except requests.RequestException as e:
                print(f"[重试 {attempt}/{self.max_retries}] 请求异常: {e}")
                if self.backoff == "price":
                    time.sleep(min(6.0, 1.0 * attempt) + random.uniform(0.3, 1.2))
                elif attempt < self.max_retries:
                    time.sleep(config.CRAWLER_CONFIG["min_interval"] * 5 * attempt)
        return None

    def init_session(self) -> bool:
        """访问 leftTicket/init 获取 Cookie"""
        resp = self.get("https://kyfw.12306.cn/otn/leftTicket/init")
        print("[Session] 会话初始化" + ("成功" if resp else "失败"))
        return resp is not None


# ============================================================
# tickets 子命令：余票（基础库）采集
# ============================================================

class TicketCrawler(HttpBase):
    """车站 → 车次 → 经停站 → 物化视图 → 同车映射 → 体检"""

    def __init__(self):
        super().__init__()
        self.query_date = config.get_query_date()
        self.stations: Dict[str, str] = {}            # station_id → name（含 @自动注册）
        self.name_to_id: Dict[str, str] = {}          # 去非汉字站名 → id
        self.g_trains: Dict[str, str] = {}            # train_num → train_no
        self.failed_details: List[str] = []           # 详情接口失败车次

    # ---- 1. 车站列表 ----
    def collect_stations(self) -> int:
        """station_name.js：@拼音|站名|电报码|拼音全称|简称|序号"""
        resp = self.get("https://kyfw.12306.cn/otn/resources/js/framework/station_name.js",
                        headers={"Referer": "https://kyfw.12306.cn/otn/leftTicket/init"})
        if resp is None:
            return 0
        m = re.search(r"var station_names\s*=\s*'([^']+)'", resp.text)
        if not m:
            print("[车站] 无法解析 station_names")
            return 0
        for entry in m.group(1).split("@"):
            parts = entry.split("|")
            if len(parts) >= 6 and parts[1].strip() and parts[2].strip():
                self.stations[parts[2].strip()] = parts[1].strip()
                self.name_to_id[_norm_station(parts[1])] = parts[2].strip()
        print(f"[车站] 采集完成，共 {len(self.stations)} 个车站")
        return len(self.stations)

    def save_stations(self, conn: sqlite3.Connection):
        cur = conn.cursor()
        cur.execute("DELETE FROM station_trains")   # 外键依赖序：先依赖表后主表
        cur.execute("DELETE FROM train_stops")
        cur.execute("DELETE FROM stations")
        cur.executemany("INSERT OR REPLACE INTO stations (station_id, station_name) VALUES (?, ?)",
                        list(self.stations.items()))
        conn.commit()
        print(f"[数据库] 已写入 {len(self.stations)} 个车站")

    # ---- 2. search API 枚举 G 车次 ----
    def _search_by_prefix(self, prefix: str) -> Dict[str, str]:
        """keyword=G{prefix} 部分匹配（G10 → G10/G100/G1001…）"""
        resp = self.get("https://search.12306.cn/search/v1/train/search",
                        params={"keyword": f"G{prefix}",
                                "date": datetime.now().strftime("%Y%m%d")},
                        headers={"Referer": "https://kyfw.12306.cn/"})
        if resp is None:
            return {}
        try:
            items = resp.json().get("data") or []
        except (json.JSONDecodeError, ValueError):
            return {}
        out = {}
        for it in items:
            code, no = it.get("station_train_code", ""), it.get("train_no", "")
            if code.startswith("G") and no:
                out.setdefault(code, no)
        return out

    def discover_g_trains(self, conn: sqlite3.Connection) -> int:
        """枚举 G10-G99 覆盖全部两位前缀，再补 G1-G9 本身（逐前缀打印，无新增不打）"""
        self.g_trains = {}
        for tens in range(1, 10):
            if _SHUTDOWN:
                break
            for ones in range(10):
                if _SHUTDOWN:
                    break
                prefix = f"{tens}{ones}"
                trains = self._search_by_prefix(prefix)
                new_count = sum(1 for tn in trains if tn not in self.g_trains)
                if not new_count:
                    continue
                for tn, no in trains.items():
                    self.g_trains.setdefault(tn, no)
                print(f"[车次]   G{prefix} → 新增 {new_count}，累计 {len(self.g_trains)}")
        for d in range(1, 10):
            if _SHUTDOWN:
                break
            exact = self._search_by_prefix(str(d)).get(f"G{d}")
            if exact:
                self.g_trains[f"G{d}"] = exact
                print(f"  → 收录 G{d}: {exact}")
        print(f"[车次] 发现完成，共 {len(self.g_trains)} 个 G 车次")

        conn.execute("DELETE FROM trains")
        conn.executemany("INSERT OR REPLACE INTO trains (train_num, train_no) VALUES (?, ?)",
                         list(self.g_trains.items()))
        conn.commit()
        return len(self.g_trains)

    # ---- 3. 经停站 ----
    def collect_train_stops(self, train_num: str, train_no: str) -> Optional[List[Dict]]:
        """queryByTrainNo 单车次经停；时间字段 arrive→depart→start→begin 取首个 HH:MM，
        无有效时间留空（不写 "--" 脏数据，由体检/清理环节处理）"""
        resp = self.get("https://kyfw.12306.cn/otn/czxx/queryByTrainNo",
                        params={"train_no": train_no,
                                "from_station_telecode": "BJP",
                                "to_station_telecode": "SHH",
                                "depart_date": self.query_date},
                        headers={"Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
                                 "X-Requested-With": "XMLHttpRequest"})
        if resp is None:
            self.failed_details.append(train_num)
            return None
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            self.failed_details.append(train_num)
            return None
        rows = (data.get("data") or {}).get("data") or [] if data.get("status") else []
        if not rows:
            self.failed_details.append(train_num)
            return None

        def _valid(t: str) -> bool:
            return bool(t and re.fullmatch(r"\d{2}:\d{2}", t.strip()))

        stops = []
        for item in rows:
            name = item.get("station_name") or ""
            arrive = (item.get("arrive_time") or "").strip()
            depart = (item.get("depart_time") or "").strip()
            stop_time = next((t for t in (arrive, depart,
                                          (item.get("start_time") or "").strip(),
                                          (item.get("begin_time") or "").strip()) if _valid(t)), "")
            sid = self.name_to_id.get(_norm_station(name), "")
            if not sid:   # 未知站自动注册（@前缀标记，防外键失败；不覆盖合法映射）
                sid = f"@{name}"
                self.stations.setdefault(sid, name)
                self.name_to_id.setdefault(_norm_station(name), sid)
            stops.append({"stop_no": item.get("station_no", 0), "station_name": name,
                          "station_id": sid, "stop_time": stop_time})
        if len(stops) < 2:
            self.failed_details.append(train_num)
            return None
        return stops

    def collect_all_stops(self, conn: sqlite3.Connection) -> int:
        """遍历全部车次写经停；批量写入期间关闭 FK（结束先 rollback 再恢复）；
        详情失败车次从 trains 删除（防下游读到空经停）"""
        cur = conn.cursor()
        total = len(self.g_trains)
        count = 0
        try:
            cur.execute("PRAGMA foreign_keys = OFF")
            cur.execute("DELETE FROM train_stops")
            cur.execute("DELETE FROM stations WHERE station_id LIKE '@%'")
            for idx, (tn, no) in enumerate(self.g_trains.items(), 1):
                if _SHUTDOWN:
                    print(f"[经停] 收到中断信号，停止遍历（已处理 {idx - 1}/{total}）")
                    break
                if idx % 20 == 0:
                    print(f"[经停] 进度 {idx}/{total}")
                stops = self.collect_train_stops(tn, no)
                if stops is None:
                    cur.execute("DELETE FROM trains WHERE train_num = ?", (tn,))
                    continue
                for s in stops:
                    if s["station_id"].startswith("@"):
                        cur.execute("INSERT OR IGNORE INTO stations (station_id, station_name) "
                                    "VALUES (?, ?)", (s["station_id"], s["station_name"]))
                    try:
                        cur.execute("""INSERT OR REPLACE INTO train_stops
                                       (train_num, stop_no, station_id, station_name, stop_time)
                                       VALUES (?, ?, ?, ?, ?)""",
                                    (tn, s["stop_no"], s["station_id"],
                                     s["station_name"], s["stop_time"]))
                    except sqlite3.Error as e:
                        print(f"[经停] 跳过异常记录 {tn} 第{s['stop_no']}站: {e}")
                        continue
                    count += 1
                if idx % 50 == 0:
                    conn.commit()
            conn.commit()
        finally:
            try:
                conn.rollback()   # 结束未决事务，否则 PRAGMA 被静默忽略
            except Exception:
                pass
            cur.execute("PRAGMA foreign_keys = ON")
        print(f"[经停] 采集完成，共 {count} 条记录")
        return count

    # ---- 4. 同车多号映射 ----
    def build_same_train_map(self, conn: sqlite3.Connection):
        """经停序列（站名+时刻）完全一致 → 同一物理列车。缺有效时间的车次不参与分组
        （key 会退化为纯站名序列，可能误合并经停相同但时刻不同的合法车）。"""
        per_train: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for tn, name, tm in conn.execute(
                "SELECT train_num, station_name, stop_time FROM train_stops "
                "ORDER BY train_num, stop_no"):
            per_train[tn].append((name, tm))   # 时刻独立保存：tm 自带冒号，不可拼串后 split 取尾
        seq_map: Dict[str, List[str]] = defaultdict(list)
        for tn, seq in per_train.items():
            if all(re.fullmatch(r"\d{2}:\d{2}", tm) for _name, tm in seq):
                seq_map["|".join(f"{name}:{tm}" for name, tm in seq)].append(tn)
        out = [{"trains": sorted(trains), "count": len(trains)}
               for trains in seq_map.values() if len(trains) > 1]
        path = db.same_train_map_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        total = sum(g["count"] for g in out)
        print(f"[同车映射] {len(out)} 组 / {total} 车次 → {path}")

    # ---- 5. 体检 ----
    def verify_data(self, conn: sqlite3.Connection) -> Dict:
        q = conn.execute
        overnight = q("""
            SELECT COUNT(DISTINCT a.train_num)
            FROM train_stops a JOIN train_stops b
              ON b.train_num = a.train_num
             AND b.stop_no = (SELECT MIN(c.stop_no) FROM train_stops c
                              WHERE c.train_num = a.train_num AND c.stop_no > a.stop_no)
            WHERE a.stop_time GLOB '[0-9][0-9]:[0-9][0-9]'
              AND b.stop_time GLOB '[0-9][0-9]:[0-9][0-9]'
              AND a.stop_time > b.stop_time
        """).fetchone()[0]   # 相邻实际 stop_no 配对（容忍断号）；GLOB 过滤脏时间
        return {
            "station_count": q("SELECT COUNT(*) FROM stations").fetchone()[0],
            "train_count": q("SELECT COUNT(*) FROM trains").fetchone()[0],
            "stop_count": q("SELECT COUNT(*) FROM train_stops").fetchone()[0],
            "trains_with_stops": q("SELECT COUNT(DISTINCT train_num) FROM train_stops").fetchone()[0],
            "station_trains_count": q("SELECT COUNT(*) FROM station_trains").fetchone()[0],
            "missing_station_id": q("SELECT COUNT(*) FROM train_stops "
                                    "WHERE station_id IS NULL OR station_id = ''").fetchone()[0],
            "missing_stop_time": q("SELECT COUNT(*) FROM train_stops "
                                   "WHERE stop_time IS NULL OR stop_time = ''").fetchone()[0],
            "overnight_train_count": overnight,
            "detail_failures": len(self.failed_details),
        }

    def run(self, railway_db: str):
        db.configure_paths(railway_db=railway_db)
        print(f"\n{'=' * 60}")
        print("  12306 高铁数据采集器（step1 tickets）")
        print(f"  基准日期: {self.query_date}（search API 枚举 G 车次）")
        print(f"{'=' * 60}")
        conn = db.init_railway_db()
        try:
            if self.collect_stations() == 0:
                sys.exit("❌ 车站列表采集失败，退出")
            self.save_stations(conn)
            if not self.init_session():
                print("[Session] 初始化异常，尝试继续")
            if self.discover_g_trains(conn) == 0:
                sys.exit("❌ 未发现任何 G 车次，退出")
            self.collect_all_stops(conn)
            db.refresh_station_trains(conn)
            self.build_same_train_map(conn)
            report = self.verify_data(conn)
            print(f"\n{'=' * 56}\n  数据验证报告\n{'=' * 56}")
            for label, key in (("车站总数", "station_count"), ("G 车次总数", "train_count"),
                               ("经停记录", "stop_count"), ("有经停车次", "trains_with_stops"),
                               ("车站-车次关联", "station_trains_count")):
                print(f"  ● {label}: {report[key]}")
            for label, key in (("缺电报码", "missing_station_id"), ("缺经停时间", "missing_stop_time"),
                               ("跨天车次", "overnight_train_count"), ("详情接口失败", "detail_failures")):
                if report[key]:
                    print(f"  ⚠ {label}: {report[key]}")
            print(f"{'=' * 56}")
            print(f"\n{'=' * 60}")
            print(f"  采集完成！基础数据库已保存至: {railway_db}")
            print(f"{'=' * 60}")
        finally:
            conn.close()


# ============================================================
# prices 子命令：票价采集
# ============================================================

def _shutdown_handler(sig, frame):
    global _SHUTDOWN
    if _SHUTDOWN:
        print("\n⚠️ 强制退出")
        sys.exit(1)
    _SHUTDOWN = True
    print("\n⏳ 正在优雅退出（再按一次 Ctrl+C 强制退出）...")


_SHUTDOWN = False

# G/D 车票价字段 → 席别；普速车兜底映射（yz/yw/rw）
SEAT_MAP_G = {"ze_price": "class2", "zy_price": "class1", "swz_price": "class0"}
SEAT_MAP_PUSU = {"yz_price": "class2", "yw_price": "class1", "rw_price": "class0"}


class PriceCollector(HttpBase):
    """相邻站对票价 → 非相邻段累加；resume/supplement/单车型/并发"""

    def __init__(self, resume=False, target_train=None, max_workers=1):
        super().__init__(backoff="price")   # prices 侧退避口径与参考逐秒对齐
        base = datetime.now()
        self.query_dates = [(base + timedelta(days=d)).strftime("%Y-%m-%d")
                            for d in config.CRAWLER_CONFIG["query_date_days_list"]]
        self.resume = resume
        self.target_train = target_train
        self.max_workers = max_workers
        self.same_train_map = db.load_same_train_map()
        self._stats_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self.failed_log = os.path.join(os.path.dirname(db._path("prices_db")),
                                       "failed_pairs.log")
        os.makedirs(os.path.dirname(self.failed_log), exist_ok=True)   # prices-db 父目录可能尚不存在
        with open(self.failed_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        self.total_pairs = self.success_count = self.fail_count = 0

    # ---- 票价查询 ----
    def query_price(self, from_id, to_id, target_train=None,
                    session=None, query_date=None) -> Optional[Dict[str, float]]:
        """查站对各席别票价（单位角→元）。匹配目标车次；高铁/动车用 G 映射，
        其余车型先普速映射、全空再按 G 映射兜底（防车型字段异常导致整车查价全空）。"""
        resp = self.get("https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice",
                        params={"leftTicketDTO.train_date": query_date,
                                "leftTicketDTO.from_station": from_id,
                                "leftTicketDTO.to_station": to_id,
                                "purpose_codes": "ADULT"},
                        headers={"Referer": "https://kyfw.12306.cn/otn/view/queryPublicIndex.html",
                                 "X-Requested-With": "XMLHttpRequest"},
                        session=session)
        if resp is None:
            return None
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        if data.get("status") is not True:
            return None
        for item in (data.get("data") or []):
            dto = item.get("queryLeftNewDTO", {})
            if target_train and dto.get("station_train_code", "") != target_train:
                continue
            is_g = (dto.get("train_class_name", "") or "") in ("高速", "动车")

            def read(mapping):
                out = {}
                for api_key, seat in mapping.items():
                    val = dto.get(api_key)
                    if val is not None:
                        try:
                            out[seat] = float(val) / 10.0
                        except (ValueError, TypeError):
                            pass
                return out

            prices = read(SEAT_MAP_G) if is_g else read(SEAT_MAP_PUSU)
            if not prices and not is_g:
                prices = read(SEAT_MAP_G)   # 兜底再探
            if prices:
                return prices
            break   # 找到目标车次但无价格数据，不遍历其余车次
        return None

    # ---- 单车次 ----
    def _crawl_single_train(self, train_num: str, already: set,
                            shared_p_conn=None) -> Dict[str, int]:
        """爬一趟车：resume 时校验站对完整性（不全则重爬）；成功且零失败后补算非相邻段。"""
        if _SHUTDOWN:
            return {"pair_ok": 0, "pair_fail": 0}
        own = shared_p_conn is None
        rw = db.get_railway_conn()
        p_conn = shared_p_conn or db.get_prices_conn()
        local = None
        if own:   # 并发线程独立 Session 并同步主 Cookie（只复制 headers 会大面积查询失败）
            local = requests.Session()
            local.headers.update(self.session.headers)
            local.cookies.update(self.session.cookies)
        try:
            stops = [{"station_id": r[0], "station_name": r[1]} for r in rw.execute(
                "SELECT station_id, station_name FROM train_stops WHERE train_num = ? "
                "ORDER BY stop_no", (train_num,))]
            if len(stops) < 2:
                print(f"[跳过] {train_num} 经停站不足 2 个")
                self._log_fail(f"{train_num}\t-\t-\t经停站不足2个")
                return {"pair_ok": 0, "pair_fail": 0}

            if self.resume and train_num in already:
                expected = len(stops) * (len(stops) - 1) // 2
                actual = p_conn.execute(
                    "SELECT COUNT(DISTINCT from_station_id || '|' || to_station_id) "
                    "FROM prices WHERE train_num = ?", (train_num,)).fetchone()[0]
                if actual >= expected:
                    print(f"[跳过] {train_num} 已爬取完整 ({actual}/{expected})")
                    return {"pair_ok": 0, "pair_fail": 0}
                print(f"[续爬] {train_num} 数据不全 ({actual}/{expected})，重新爬取")

            print(f"[爬取] {train_num} ...")
            p_conn.execute("DELETE FROM prices WHERE train_num = ?", (train_num,))
            p_conn.commit()
            ok, fail = self._crawl_pairs(p_conn, train_num, stops,
                                         session=local if own else None)
            if ok > 0:
                print(f"[完成] {train_num}: {ok}/{ok + fail} 站对成功")
                if fail == 0:
                    self._compute_train_pairs(p_conn, train_num, stops)
            else:
                print(f"[失败] {train_num}: 全部 {fail} 个站对均无数据")
            return {"pair_ok": ok, "pair_fail": fail}
        finally:
            rw.close()
            if own:
                p_conn.close()

    def _crawl_pairs(self, p_conn, train_num, stops, only_missing=None,
                     session=None) -> Tuple[int, int]:
        """相邻站对逐一爬取。每站对：当前生效日期起逐天重试 × 3轮 × 同组车次各5次；
        同组车次（same_trains.json）任一查到即用（记 effective 车次优先下次尝试）。
        全日期失败：全量模式记剩余站对为失败并 break；补充模式只计实际尝试失败。"""
        siblings = self.same_train_map.get(train_num, [])
        all_trains = [train_num] + siblings
        effective = train_num
        pair_ok = pair_fail = 0
        date_idx = 0

        for i in range(len(stops) - 1):
            if _SHUTDOWN:
                break
            f, t = stops[i], stops[i + 1]
            if only_missing is not None and (f["station_id"], t["station_id"]) not in only_missing:
                continue

            prices = None
            for di in range(date_idx, len(self.query_dates)):
                crawl_date = self.query_dates[di]
                if di != date_idx:
                    print(f"  ⟳ 换日期 {crawl_date} 重试 {f['station_name']}→{t['station_name']}")
                for round_i in range(3):
                    ordered = [effective] + [x for x in all_trains if x != effective]
                    for tn in ordered:
                        for attempt in range(5):
                            prices = self.query_price(f["station_id"], t["station_id"], tn,
                                                      session=session, query_date=crawl_date)
                            if prices is not None:
                                if tn != train_num:
                                    print(f"  ↪ 用 {tn} 查到 {f['station_name']}→{t['station_name']}")
                                date_idx = di
                                effective = tn
                                break
                            print(f"  ⟳ {f['station_name']}→{t['station_name']} [{tn}] 第{attempt + 1}次无数据")
                        if prices is not None:
                            break
                    if prices is not None:
                        break
                    if round_i < 2:
                        print(f"  ⟳ 第{round_i + 1}轮无果，进入下一轮")
                if prices is not None:
                    break

            if prices is None:
                print(f"  ✗ {f['station_name']}→{t['station_name']} 各日期均无数据，放弃 {train_num}")
                with self._log_lock:
                    with open(self.failed_log, "a", encoding="utf-8") as fh:
                        fh.write(f"{train_num}\t{f['station_name']}({f['station_id']})\t"
                                 f"{t['station_name']}({t['station_id']})\t全车放弃\n")
                        if only_missing is None:   # 全量模式：剩余站对如实记未爬取
                            for k in range(i + 1, len(stops) - 1):
                                r, nxt = stops[k], stops[k + 1]
                                fh.write(f"{train_num}\t{r['station_name']}({r['station_id']})\t"
                                         f"{nxt['station_name']}({nxt['station_id']})\t未爬取\n")
                pair_fail = (len(stops) - 1 - i) if only_missing is None else 1
                break

            for seat, price in prices.items():
                db.save_price(p_conn, train_num, f["station_id"], t["station_id"], seat, price)
            p_conn.commit()
            pair_ok += 1
            print(f"  ✓ {f['station_name']}→{t['station_name']} {prices}")
        return pair_ok, pair_fail

    def _compute_train_pairs(self, p_conn, train_num, stops) -> int:
        """非相邻段票价 = 相邻段逐席别累加（相邻段不齐则跳过，返回补算条数）"""
        existing_adj = {(r[0], r[1]) for r in p_conn.execute(
            "SELECT DISTINCT from_station_id, to_station_id FROM prices WHERE train_num = ?",
            (train_num,))}
        all_adj = {(stops[i]["station_id"], stops[i + 1]["station_id"])
                   for i in range(len(stops) - 1)}
        if all_adj - existing_adj:
            return 0
        adj = {(r[0], r[1], r[2]): r[3] for r in p_conn.execute(
            "SELECT from_station_id, to_station_id, seat, price FROM prices "
            "WHERE train_num = ?", (train_num,))}
        seats = sorted({r[0] for r in p_conn.execute(
            "SELECT DISTINCT seat FROM prices WHERE train_num = ?", (train_num,))})
        computed = 0
        n = len(stops)
        for i in range(n):
            for j in range(i + 2, n):
                f_id, t_id = stops[i]["station_id"], stops[j]["station_id"]
                for seat in seats:
                    total, valid = 0.0, True
                    for k in range(i, j):
                        key = (stops[k]["station_id"], stops[k + 1]["station_id"], seat)
                        if key not in adj:
                            valid = False
                            break
                        total += adj[key]
                    if valid:
                        db.save_price(p_conn, train_num, f_id, t_id, seat, round(total, 2))
                        computed += 1
        p_conn.commit()
        if computed:
            print(f"  📐 已计算 {computed} 条非相邻段票价")
        return computed

    def _log_fail(self, line: str):
        with self._log_lock:
            with open(self.failed_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ---- 主流程 ----
    def run(self):
        db.init_prices_table()
        print("[DB] prices 表已就绪")
        if not self.init_session():
            sys.exit("❌ 会话初始化失败，终止")
        rw = db.get_railway_conn()
        if self.target_train:
            train_list = [self.target_train]
        else:
            train_list = [r[0] for r in rw.execute("SELECT train_num FROM trains ORDER BY train_num")]
        rw.close()
        already = set()
        if self.resume:
            p = db.get_prices_conn()
            already = db.get_crawled_price_trains(p)
            p.close()
            print(f"[续爬] 已有 {len(already)} 趟车次已爬取，跳过完整车次")

        workers = self.max_workers if not self.target_train else 1
        if workers > 1:
            print(f"[并发] {workers} 线程 × {len(train_list)} 车次")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._crawl_single_train, tn, already): tn
                        for tn in train_list if not _SHUTDOWN}
                for fut in as_completed(futs):
                    try:
                        r = fut.result()
                        with self._stats_lock:
                            self.total_pairs += r["pair_ok"] + r["pair_fail"]
                            self.success_count += r["pair_ok"]
                            self.fail_count += r["pair_fail"]
                    except Exception as e:
                        print(f"[异常] {futs.get(fut, '?')}: {e}")
        else:
            p_conn = db.get_prices_conn()
            try:
                for tn in train_list:
                    if _SHUTDOWN:
                        print("[中止] 检测到退出信号")
                        break
                    r = self._crawl_single_train(tn, already, shared_p_conn=p_conn)
                    self.total_pairs += r["pair_ok"] + r["pair_fail"]
                    self.success_count += r["pair_ok"]
                    self.fail_count += r["pair_fail"]
            finally:
                p_conn.close()
        self._report()

    def supplement(self):
        """补缺：候选=trains 全集 ∪ prices 已有号（全量爬取全失败的零票价车次
        也能被扫到）；只爬缺失相邻站对，不删已有数据"""
        db.init_prices_table()
        print("[DB] prices 表已就绪")
        if not self.init_session():
            sys.exit("❌ 会话初始化失败，终止")
        rw = db.get_railway_conn()
        p_conn = db.get_prices_conn()
        train_set = {r[0] for r in rw.execute("SELECT train_num FROM trains")}
        train_set.update(r[0] for r in p_conn.execute("SELECT DISTINCT train_num FROM prices"))
        supplemented = 0
        for tn in sorted(train_set):
            if _SHUTDOWN:
                break
            if self.target_train and tn != self.target_train:
                continue
            stops = [{"station_id": r[0], "station_name": r[1]} for r in rw.execute(
                "SELECT station_id, station_name FROM train_stops WHERE train_num = ? "
                "ORDER BY stop_no", (tn,))]
            if len(stops) < 2:
                continue
            existing = {(r[0], r[1]) for r in p_conn.execute(
                "SELECT DISTINCT from_station_id, to_station_id FROM prices WHERE train_num = ?",
                (tn,))}
            missing = [(stops[i], stops[i + 1]) for i in range(len(stops) - 1)
                       if (stops[i]["station_id"], stops[i + 1]["station_id"]) not in existing]
            if not missing:
                continue
            print(f"[补充] {tn}: {len(missing)} 个缺失站对")
            ok, fail = self._crawl_pairs(p_conn, tn, stops,
                                         only_missing={(f["station_id"], t["station_id"])
                                                       for f, t in missing})
            supplemented += ok
            self.total_pairs += ok + fail
            self.success_count += ok
            self.fail_count += fail
            if fail == 0:
                self._compute_train_pairs(p_conn, tn, stops)
        rw.close()
        p_conn.close()
        print(f"[补充] 完成，补充了 {supplemented} 个站对")

    def _report(self):
        print("=" * 50)
        print(f"爬取完成：总站对 {self.total_pairs}，成功 {self.success_count}，失败 {self.fail_count}"
              + (f"，成功率 {self.success_count / self.total_pairs * 100:.1f}%"
                 if self.total_pairs else ""))
        print(f"失败明细：{self.failed_log}")


# ============================================================
# cleanup / overnight / check 子命令
# ============================================================

def _rw_path() -> str:
    return db._path("railway_db")


def _pr_path() -> str:
    return db._path("prices_db")


def delete_train_completely(train_num: str) -> dict:
    """从 prices.db / railway.db(trains+train_stops) / same_trains.json 全量删除一趟车。
    同车组剩余 <2 则整组删除。"""
    details = []
    try:
        pr = sqlite3.connect(_pr_path())
        pr.execute("DELETE FROM prices WHERE train_num = ?", (train_num,))
        pr.commit()
        pr.close()
        details.append("✓ prices.db")
    except Exception as e:
        details.append(f"✗ prices.db: {e}")
    try:
        rw = sqlite3.connect(_rw_path())
        rw.execute("PRAGMA foreign_keys = ON")
        rw.execute("DELETE FROM train_stops WHERE train_num = ?", (train_num,))
        rw.execute("DELETE FROM trains WHERE train_num = ?", (train_num,))
        rw.commit()
        rw.close()
        details.append("✓ railway.db (trains + train_stops)")
    except Exception as e:
        details.append(f"✗ railway.db: {e}")
    same_path = db.same_train_map_path()
    if os.path.exists(same_path):
        try:
            with open(same_path, "r", encoding="utf-8") as f:
                groups = json.load(f)
            kept = []
            for g in groups:
                if train_num in g.get("trains", []):
                    rest = sorted(t for t in g["trains"] if t != train_num)
                    if len(rest) >= 2:
                        g["trains"], g["count"] = rest, len(rest)
                        kept.append(g)
                    # 剩余 <2：整组删除
                else:
                    kept.append(g)
            with open(same_path, "w", encoding="utf-8") as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
            details.append("✓ same_trains.json")
        except Exception as e:
            details.append(f"✗ same_trains.json: {e}")
    return {"success": all(not d.startswith("✗") for d in details), "details": details}


def inspect_train(train_num: str) -> dict:
    """单趟车数据完整性：trains 在册 / 经停数 / 票价站对数 vs 理论 n(n-1)/2"""
    rw = sqlite3.connect(_rw_path())
    in_trains = rw.execute("SELECT 1 FROM trains WHERE train_num = ?",
                           (train_num,)).fetchone() is not None
    stop_count = rw.execute("SELECT COUNT(*) FROM train_stops WHERE train_num = ?",
                            (train_num,)).fetchone()[0]
    rw.close()
    stops_ok = stop_count >= 2
    expected = stop_count * (stop_count - 1) // 2 if stops_ok else 0
    pr = sqlite3.connect(_pr_path())
    actual = pr.execute(
        "SELECT COUNT(DISTINCT from_station_id || '|' || to_station_id) "
        "FROM prices WHERE train_num = ?", (train_num,)).fetchone()[0]
    seats = [r[0] for r in pr.execute(
        "SELECT DISTINCT seat FROM prices WHERE train_num = ? ORDER BY seat", (train_num,))]
    pr.close()
    return {"train_num": train_num, "in_trains": in_trains, "stop_count": stop_count,
            "stops_ok": stops_ok, "expected_pairs": expected, "actual_pairs": actual,
            "pairs_ok": actual >= expected, "complete": stops_ok and actual >= expected,
            "seat_types": seats}


def find_duplicate_trains() -> dict:
    """同车多号只读清单：每组保留主号（trains 中票价记录最多，并列取字典序最小）；
    纯孤儿 = 在 train_stops 但不在 trains 且不属于任何同车组"""
    rw = sqlite3.connect(_rw_path())
    pr = sqlite3.connect(_pr_path())
    try:
        trains_set = {r[0] for r in rw.execute("SELECT train_num FROM trains")}
        stops_set = {r[0] for r in rw.execute("SELECT DISTINCT train_num FROM train_stops")}

        def price_count(tn):
            return pr.execute("SELECT COUNT(*) FROM prices WHERE train_num=?",
                              (tn,)).fetchone()[0]

        groups = []
        same_path = db.same_train_map_path()
        if os.path.exists(same_path):
            with open(same_path, "r", encoding="utf-8") as f:
                groups = json.load(f)
        canonical, duplicates, members = {}, [], set()
        for g in groups:
            ms = g.get("trains", [])
            members.update(ms)
            in_trains = [t for t in ms if t in trains_set]
            if not in_trains:          # 整组异常：全部视为重复号
                duplicates.extend(ms)
                continue
            keep = min(in_trains, key=lambda t: (-price_count(t), t))
            canonical[keep] = [t for t in ms if t != keep]
            duplicates.extend(canonical[keep])
        return {"canonical": canonical, "duplicates": sorted(duplicates),
                "pure_orphans": sorted(stops_set - trains_set - members),
                "group_count": len(groups)}
    finally:
        rw.close()
        pr.close()


def compute_missing_pairs(train_num: str) -> int:
    """离线补算非相邻段票价（相邻段齐全时累加；crawl_date 必填 NOT NULL）。"""
    rw = sqlite3.connect(_rw_path())
    stops = [r[0] for r in rw.execute(
        "SELECT station_id FROM train_stops WHERE train_num = ? ORDER BY stop_no",
        (train_num,))]
    rw.close()
    if len(stops) < 2:
        return 0
    pr = sqlite3.connect(_pr_path())
    try:
        adj = {(r[0], r[1], r[2]): r[3] for r in pr.execute(
            "SELECT from_station_id, to_station_id, seat, price FROM prices WHERE train_num = ?",
            (train_num,))}
        seats = sorted({r[2] for r in adj})
        if not seats:
            return 0
        all_adj = {(stops[i], stops[i + 1]) for i in range(len(stops) - 1)}
        if all_adj - {(f, t) for (f, t, _s) in adj}:
            return 0   # 相邻段不全，无法离线补算（需 --supplement 联网补爬）
        today = datetime.now().strftime("%Y-%m-%d")
        computed = 0
        n = len(stops)
        for i in range(n):
            for j in range(i + 2, n):
                for seat in seats:
                    total, valid = 0.0, True
                    for k in range(i, j):
                        key = (stops[k], stops[k + 1], seat)
                        if key not in adj:
                            valid = False
                            break
                        total += adj[key]
                    if valid:
                        pr.execute(
                            "INSERT OR REPLACE INTO prices (train_num, from_station_id, "
                            "to_station_id, seat, price, crawl_date) VALUES (?, ?, ?, ?, ?, ?)",
                            (train_num, stops[i], stops[j], seat, round(total, 2), today))
                        computed += 1
        pr.commit()
    finally:
        pr.close()
    return computed


def _rebuild_and_report(deleted: list, skipped: list, need_rebuild: bool):
    """重建物化视图 + 打印清理报告"""
    if need_rebuild:
        print(f"\n{'─' * 50}\n  重建 station_trains 物化视图...")
        try:
            conn = sqlite3.connect(_rw_path())
            db.refresh_station_trains(conn)
            conn.close()
            print("  ✅ 重建完成")
        except Exception as e:
            print(f"  ❌ 重建失败: {e}")
    print(f"\n{'=' * 56}\n  清理报告\n{'=' * 56}")
    if deleted:
        print(f"  已删除 ({len(deleted)}): {', '.join(deleted)}")
    if skipped:
        print(f"  已跳过 ({len(skipped)}): {', '.join(skipped)}")
    print(f"{'=' * 56}")


def run_cleanup(check_only: bool):
    """一键清理：①同车多号去重 ②非相邻段补算 ③删仍不全 ④重建；--check 只读"""
    plan = find_duplicate_trains()
    remove = sorted(set(plan["duplicates"]) | set(plan["pure_orphans"]))
    print("=" * 60)
    print(f"  同车多号去重：{plan['group_count']} 组，保留主号 {len(plan['canonical'])} 个，"
          f"待删 {len(remove)} 个")
    print("=" * 60)
    if check_only:
        if remove:
            print(f"  重复号/孤儿：{', '.join(remove)}")
        else:
            print("  无重复车次 ✅")
    else:
        for tn in remove:
            result = delete_train_completely(tn)
            print(f"  {'✅' if result['success'] else '⚠️'} 删除 {tn}: "
                  f"{'; '.join(result['details'])}")

    # 票价完整性
    rw = sqlite3.connect(_rw_path())
    all_trains = [r[0] for r in rw.execute("SELECT train_num FROM trains ORDER BY train_num")]
    rw.close()
    incomplete = [info for tn in all_trains if not (info := inspect_train(tn))["complete"]]
    if check_only:
        if incomplete:
            print(f"\n票价不全车次 {len(incomplete)} 个: "
                  f"{', '.join(i['train_num'] for i in incomplete)}")
        else:
            print("\n票价完整性：所有车次完整 ✅")
        print(f"\n共发现待处理项：去重 {len(remove)} + 票价不全 {len(incomplete)}"
              + ("（当前 --check 只读）" if check_only else ""))
        return
    if not incomplete:
        print("\n✅ 所有车次数据完整，无需清理。")
        _rebuild_and_report(remove, [], bool(remove))
        return

    print(f"\n票价不全 {len(incomplete)} 车次，先离线补算非相邻段...")
    for info in incomplete:
        added = compute_missing_pairs(info["train_num"])
        if added:
            print(f"  ✓ {info['train_num']}: 补算 {added} 条")
    incomplete = [info for info in incomplete
                  if not inspect_train(info["train_num"])["complete"]]
    if not incomplete:
        print("✅ 补算后所有车次完整。")
        _rebuild_and_report(remove, [], True)
        return

    print(f"\n自动删除仍不全的 {len(incomplete)} 个车次...")
    deleted, need_rebuild = [], False
    for info in incomplete:
        tn = info["train_num"]
        result = delete_train_completely(tn)
        deleted.append(tn)
        status = "✅" if result["success"] else "⚠️"
        print(f"  {status} {tn}: {'; '.join(result['details'])}")
        if result["success"]:
            need_rebuild = True
    _rebuild_and_report(deleted, [], need_rebuild)


def run_overnight(apply: bool):
    """跨天车清理：相邻实际 stop_no 配对 + 双侧 HH:MM + 时间倒退判定；--apply 才删"""
    print("=" * 64)
    print("  清理跨天车次（当日完成约束：相邻经停时间倒退，如 23:20→00:07）")
    print(f"  模式：{'【执行删除】' if apply else '【仅显示】'}（--apply 执行删除）")
    print("=" * 64)
    overnight = run_overnight_scan()
    if not overnight:
        print("\n✅ 没有跨天车次。")
        return
    print(f"\n发现 {len(overnight)} 个跨天车次：\n")
    for tn in sorted(overnight):
        spots = overnight[tn]
        print(f"  {tn}: " + "；".join(f"第{s}站 {a}→{b}" for s, a, b in spots))
    if not apply:
        print("\n（仅显示。确认后加 --apply 执行删除）")
        return
    print("\n正在删除 ...\n")
    failed = []
    for tn in sorted(overnight):
        result = delete_train_completely(tn)
        print(f"  {'✅' if result['success'] else '❌'} {tn}: {'; '.join(result['details'])}")
        if not result["success"]:
            failed.append(tn)
    conn = sqlite3.connect(_rw_path())
    db.refresh_station_trains(conn)
    conn.close()
    # 复查
    remain = len(run_overnight_scan())
    print(f"\nstation_trains 已重建。删除后剩余跨天车次: {remain}")
    if failed:
        print(f"⚠️ 删除失败车次: {failed}")
    if remain:
        print("⚠️ 仍有跨天车次，请复查")


def run_overnight_scan() -> dict:
    """独立扫描跨天车次（复查用）"""
    rw = sqlite3.connect(_rw_path())
    rows = rw.execute("""
        SELECT a.train_num, a.stop_no, a.stop_time, b.stop_time
        FROM train_stops a
        JOIN train_stops b ON b.train_num = a.train_num
         AND b.stop_no = (SELECT MIN(c.stop_no) FROM train_stops c
                          WHERE c.train_num = a.train_num AND c.stop_no > a.stop_no)
    """).fetchall()
    rw.close()
    out: Dict[str, list] = {}
    for tn, no, t_from, t_to in rows:
        if (re.fullmatch(r"\d{2}:\d{2}", t_from or "")
                and re.fullmatch(r"\d{2}:\d{2}", t_to or "") and t_from > t_to):
            out.setdefault(tn, []).append((no, t_from, t_to))
    return out


def run_check():
    """只读体检：基础库计数 + 票价库概览 + 突出问题清单"""
    rw = sqlite3.connect(_rw_path())
    q = rw.execute
    print(f"{'=' * 56}\n  数据体检报告（只读）\n{'=' * 56}")
    stats = {
        "stations": q("SELECT COUNT(*) FROM stations").fetchone()[0],
        "trains": q("SELECT COUNT(*) FROM trains").fetchone()[0],
        "train_stops": q("SELECT COUNT(*) FROM train_stops").fetchone()[0],
        "station_trains": q("SELECT COUNT(*) FROM station_trains").fetchone()[0],
        "missing_id": q("SELECT COUNT(*) FROM train_stops WHERE station_id IS NULL "
                        "OR station_id = ''").fetchone()[0],
        "missing_time": q("SELECT COUNT(*) FROM train_stops WHERE stop_time IS NULL "
                          "OR stop_time = ''").fetchone()[0],
    }
    rw.close()
    for k, label in (("stations", "车站"), ("trains", "G 车次"), ("train_stops", "经停记录"),
                     ("station_trains", "车站-车次关联")):
        print(f"  ● {label}: {stats[k]}")
    if stats["missing_id"] or stats["missing_time"]:
        print(f"  ⚠ 缺电报码 {stats['missing_id']}，缺经停时间 {stats['missing_time']}")
    pr = sqlite3.connect(_pr_path())
    price_trains = pr.execute("SELECT COUNT(DISTINCT train_num) FROM prices").fetchone()[0]
    price_rows = pr.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    pr.close()
    print(f"  ● 有票价车次: {price_trains}，票价记录: {price_rows}")
    plan = find_duplicate_trains()
    remove = sorted(set(plan["duplicates"]) | set(plan["pure_orphans"]))
    if remove:
        print(f"  ⚠ 同车多号/孤儿待去重: {len(remove)} 个（cleanup 处理）")
    overnight = run_overnight_scan()
    if overnight:
        print(f"  ⚠ 跨天车次: {len(overnight)} 个（overnight --apply 处理）")
    if not (remove or overnight or stats["missing_id"] or stats["missing_time"]):
        print("  ✅ 未发现问题")


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="step1 数据爬取与处理（tickets/prices/cleanup/overnight/check/all）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_rw(p, need_prices=True):
        p.add_argument("--railway-db", required=True, help="基础数据库路径")
        if need_prices:
            p.add_argument("--prices-db", required=True, help="票价数据库路径")

    p = sub.add_parser("tickets", help="采集余票基础库（车站/车次/经停/物化/同车映射）")
    p.add_argument("--railway-db", required=True)

    p = sub.add_parser("prices", help="采集票价库（相邻站对 + 非相邻段累加）")
    add_rw(p)
    p.add_argument("--resume", action="store_true", help="断点续爬（跳过完整车次）")
    p.add_argument("--supplement", action="store_true", help="补缺模式（只爬缺失相邻站对）")
    p.add_argument("--train", default=None, help="仅爬指定车次（如 G1）")
    p.add_argument("--workers", type=int, default=1, help="并发线程数（默认 1，建议 3~5）")

    p = sub.add_parser("cleanup", help="一键清理（去重→补算→删仍不全→重建）；--check 只读")
    add_rw(p)
    p.add_argument("--check", action="store_true", help="只读体检，不写库")

    p = sub.add_parser("overnight", help="跨天车清理（默认仅显示）")
    add_rw(p)
    p.add_argument("--apply", action="store_true", help="执行删除")

    p = sub.add_parser("check", help="只读体检")
    p.add_argument("--railway-db", required=True)
    p.add_argument("--prices-db", required=True)

    p = sub.add_parser("all", help="全流程：tickets → prices → cleanup → overnight → check")
    add_rw(p)

    args = ap.parse_args()

    if args.cmd == "tickets":
        db.configure_paths(railway_db=args.railway_db)
        TicketCrawler().run(args.railway_db)
        return
    if args.cmd == "check":
        db.configure_paths(railway_db=args.railway_db, prices_db=args.prices_db)
        run_check()
        return

    # prices/cleanup/overnight/all 需要双库路径；prices 采集安装优雅退出处理
    db.configure_paths(railway_db=args.railway_db, prices_db=args.prices_db)
    if args.cmd in ("prices", "all"):
        signal.signal(signal.SIGINT, _shutdown_handler)

    if args.cmd == "prices":
        collector = PriceCollector(resume=args.resume, target_train=args.train,
                                   max_workers=max(1, args.workers))
        collector.supplement() if args.supplement else collector.run()
    elif args.cmd == "cleanup":
        run_cleanup(check_only=args.check)
    elif args.cmd == "overnight":
        run_overnight(apply=args.apply)
    elif args.cmd == "all":
        print("===== [1/5] tickets =====")
        TicketCrawler().run(args.railway_db)
        if _SHUTDOWN:
            return
        print("===== [2/5] prices =====")
        PriceCollector().run()
        if _SHUTDOWN:
            return
        print("===== [3/5] cleanup =====")
        run_cleanup(check_only=False)
        if _SHUTDOWN:
            return
        print("===== [4/5] overnight =====")
        run_overnight(apply=True)
        if _SHUTDOWN:
            return
        print("===== [5/5] check =====")
        run_check()


if __name__ == "__main__":
    main()
