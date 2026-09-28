"""
数据库层：连接管理、余票/票价读写、题目元数据（含 NL 独立产物与流水线 state）。

所有数据流路径由 step 入口经 configure_paths() 注入（None 报错），本模块不读
任何写死的路径。语义与参考项目一致，仅按 CLI 架构裁剪了无消费方的函数。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# 三张余票表（席别）
TICKET_TABLES = ["class0", "class1", "class2"]

# ------------------------------------------------------------
# 路径注入（进程级；step 入口在 main() 中调用一次）
# ------------------------------------------------------------
_PATHS: Dict[str, Optional[str]] = {
    "railway_db": None,    # 基础库（stations/trains/train_stops/station_trains）
    "prices_db": None,     # 票价库
    "question_dir": None,  # 题目目录（*.db）
    "metadata": None,      # 题目元数据
    "metadata_nl": None,   # NL 独立产物
    "data_dir": None,      # same_trains.json 所在目录
}


class PathsNotConfigured(RuntimeError):
    """数据流路径未注入（入口脚本必须先调用 configure_paths）"""


def configure_paths(railway_db: str = None, prices_db: str = None,
                    question_dir: str = None, metadata: str = None,
                    metadata_nl: str = None, data_dir: str = None):
    """注入数据流路径（None=保持已设值）。缺省推导：
    data_dir ← railway_db 所在目录；metadata ← question_dir/metadata.json；
    metadata_nl ← question_dir/metadata_nl.json。"""
    if railway_db:
        _PATHS["railway_db"] = os.path.abspath(railway_db)
    if prices_db:
        _PATHS["prices_db"] = os.path.abspath(prices_db)
    if question_dir:
        _PATHS["question_dir"] = os.path.abspath(question_dir)
    if data_dir:
        _PATHS["data_dir"] = os.path.abspath(data_dir)
    if not _PATHS["data_dir"] and _PATHS["railway_db"]:
        _PATHS["data_dir"] = os.path.dirname(_PATHS["railway_db"])
    if metadata:
        _PATHS["metadata"] = os.path.abspath(metadata)
    if not _PATHS["metadata"] and _PATHS["question_dir"]:
        _PATHS["metadata"] = os.path.join(_PATHS["question_dir"], "metadata.json")
    if metadata_nl:
        _PATHS["metadata_nl"] = os.path.abspath(metadata_nl)
    if not _PATHS["metadata_nl"] and _PATHS["question_dir"]:
        _PATHS["metadata_nl"] = os.path.join(_PATHS["question_dir"], "metadata_nl.json")


def _path(key: str) -> str:
    v = _PATHS.get(key)
    if not v:
        raise PathsNotConfigured(f"数据流路径未配置: {key}（先调用 database.configure_paths）")
    return v


def _ensure_parent(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def get_question_db_path(question_id: str) -> str:
    """题目数据库路径 {question_dir}/{qid}.db"""
    return os.path.join(_path("question_dir"), f"{question_id}.db")


# ============================================================
# 基础数据库（railway.db）
# ============================================================

def init_railway_db() -> sqlite3.Connection:
    """创建四张核心表并返回连接（step1 tickets 子命令使用）"""
    p = _path("railway_db")
    _ensure_parent(p)
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stations (
            station_id TEXT PRIMARY KEY,
            station_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trains (
            train_num TEXT PRIMARY KEY,
            train_no TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS train_stops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            train_num TEXT NOT NULL,
            stop_no INTEGER NOT NULL,
            station_id TEXT NOT NULL,
            station_name TEXT NOT NULL,
            stop_time TEXT NOT NULL,
            FOREIGN KEY (train_num) REFERENCES trains(train_num) ON UPDATE CASCADE,
            FOREIGN KEY (station_id) REFERENCES stations(station_id) ON UPDATE CASCADE,
            UNIQUE(train_num, stop_no)
        );
        CREATE INDEX IF NOT EXISTS idx_train_stops_train_num ON train_stops(train_num);
        CREATE INDEX IF NOT EXISTS idx_train_stops_station_id ON train_stops(station_id);
        CREATE TABLE IF NOT EXISTS station_trains (
            station_id TEXT PRIMARY KEY,
            station_name TEXT NOT NULL,
            data TEXT NOT NULL,
            FOREIGN KEY (station_id) REFERENCES stations(station_id) ON UPDATE CASCADE
        );
    """)
    conn.commit()
    return conn


def get_railway_conn() -> sqlite3.Connection:
    """基础库连接（row_factory=Row）"""
    p = _path("railway_db")
    if not os.path.exists(p):
        raise FileNotFoundError(f"基础数据库不存在: {p}，请先运行 step1_collect.py tickets")
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_train_stops(conn: sqlite3.Connection, train_num: str) -> List[Dict]:
    """车次经停站（按 stop_no 升序）：[{stop_no, station_id, station_name, stop_time}]"""
    rows = conn.execute("""
        SELECT stop_no, station_id, station_name, stop_time
        FROM train_stops WHERE train_num = ? ORDER BY stop_no
    """, (train_num,)).fetchall()
    return [dict(zip(("stop_no", "station_id", "station_name", "stop_time"), tuple(r)))
            for r in rows]


def get_routes_between(conn: sqlite3.Connection, from_id: str, to_id: str) -> List[Dict]:
    """同时经过 from/to（from 在 to 前）的车次：[{train_num, depart_time, arrive_time, duration}]"""
    rows = conn.execute("""
        SELECT s1.train_num, s1.stop_time, s2.stop_time
        FROM train_stops s1
        JOIN train_stops s2 ON s1.train_num = s2.train_num
        WHERE s1.station_id = ? AND s2.station_id = ? AND s1.stop_no < s2.stop_no
        ORDER BY s1.stop_time
    """, (from_id, to_id)).fetchall()
    return [{"train_num": tn, "depart_time": d, "arrive_time": a,
             "duration": calc_duration(d, a)} for tn, d, a in rows]


def calc_duration(depart: str, arrive: str) -> str:
    """HH:MM 差值（负数循环 +1440，≥24h 车次不回绕）；解析失败返回 --:--"""
    try:
        h1, m1 = map(int, depart.split(":"))
        h2, m2 = map(int, arrive.split(":"))
        mins = (h2 * 60 + m2) - (h1 * 60 + m1)
        while mins < 0:
            mins += 1440
        return f"{mins // 60:02d}:{mins % 60:02d}"
    except (ValueError, AttributeError):
        return "--:--"


def get_station_trains(conn: sqlite3.Connection, station_id: str) -> Optional[Dict]:
    """车站经停车次（station_trains 物化视图）：{station_id, station_name, trains: [...]}
    键名与消费方（step4 query_trains_at_station 工具）对齐。"""
    row = conn.execute(
        "SELECT station_id, station_name, data FROM station_trains WHERE station_id = ?",
        (station_id,)).fetchone()
    if row is None:
        return None
    return {"station_id": row[0], "station_name": row[1], "trains": json.loads(row[2])}


def validate_station_exists(conn: sqlite3.Connection, station_id: str) -> bool:
    return conn.execute("SELECT 1 FROM stations WHERE station_id = ?",
                        (station_id,)).fetchone() is not None


def validate_train_exists(conn: sqlite3.Connection, train_num: str) -> bool:
    return conn.execute("SELECT 1 FROM trains WHERE train_num = ?",
                        (train_num,)).fetchone() is not None


def resolve_station_name_or_id(conn: sqlite3.Connection, input_str: str) -> str:
    """站名/电报码 → 标准 station_id。依次尝试：id 直认 → 站名精确 → 去"站"精确 →
    模糊 → 去"站"模糊；都失败返回原值（由调用方报错）。"""
    s = input_str.strip()
    if validate_station_exists(conn, s):
        return s
    cur = conn.cursor()
    for name in (s, s.replace("站", "")):
        cur.execute("SELECT station_id FROM stations WHERE station_name = ?", (name,))
        row = cur.fetchone()
        if row:
            return row[0]
    for name in (s, s.replace("站", "")):
        cur.execute("SELECT station_id FROM stations WHERE station_name LIKE ? LIMIT 1",
                    (f"%{name}%",))
        row = cur.fetchone()
        if row:
            return row[0]
    return s


def refresh_station_trains(rw_conn: sqlite3.Connection):
    """从 train_stops 聚合重建 station_trains 物化视图（step1 清理/采集后调用）"""
    rows = rw_conn.execute("""
        SELECT ts.station_id, ts.station_name, ts.train_num, ts.stop_time
        FROM train_stops ts ORDER BY ts.train_num
    """).fetchall()
    grouped: Dict[str, Dict] = {}
    for station_id, station_name, train_num, stop_time in rows:
        g = grouped.setdefault(station_id, {"station_name": station_name, "trains": []})
        g["trains"].append({"train_num": train_num, "stop_time": stop_time})
    rw_conn.execute("DELETE FROM station_trains")
    for station_id, data in grouped.items():
        rw_conn.execute(
            "INSERT OR REPLACE INTO station_trains (station_id, station_name, data) VALUES (?, ?, ?)",
            (station_id, data["station_name"], json.dumps(data["trains"], ensure_ascii=False)))
    rw_conn.commit()
    print(f"[database] 已刷新 station_trains 表，共 {len(grouped)} 个车站")


# ============================================================
# 票价数据库（prices.db）
# ============================================================

def init_prices_table():
    p = _path("prices_db")
    _ensure_parent(p)
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            train_num TEXT NOT NULL,
            from_station_id TEXT NOT NULL,
            to_station_id TEXT NOT NULL,
            seat TEXT NOT NULL,
            price REAL NOT NULL,
            crawl_date TEXT NOT NULL,
            UNIQUE(train_num, from_station_id, to_station_id, seat)
        );
        CREATE INDEX IF NOT EXISTS idx_prices_train_num ON prices(train_num);
        CREATE INDEX IF NOT EXISTS idx_prices_station_pair ON prices(from_station_id, to_station_id);
    """)
    conn.commit()
    conn.close()


def get_prices_conn() -> sqlite3.Connection:
    """票价库连接（busy_timeout=30s 缓解并发写锁）"""
    conn = sqlite3.connect(_path("prices_db"), timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def save_price(conn: sqlite3.Connection, train_num: str, from_id: str,
               to_id: str, seat: str, price: float):
    """写一条票价（crawl_date 必填，NOT NULL 约束）"""
    conn.execute("""
        INSERT OR REPLACE INTO prices
        (train_num, from_station_id, to_station_id, seat, price, crawl_date)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (train_num, from_id, to_id, seat, price, datetime.now().strftime("%Y-%m-%d")))


def get_price(conn: sqlite3.Connection, train_num: str, from_id: str,
              to_id: str, seat: str) -> Optional[float]:
    row = conn.execute("""
        SELECT price FROM prices
        WHERE train_num = ? AND from_station_id = ? AND to_station_id = ? AND seat = ?
    """, (train_num, from_id, to_id, seat)).fetchone()
    return row[0] if row else None


def get_crawled_price_trains(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT DISTINCT train_num FROM prices")}


# ============================================================
# 题目数据库（question/*.db）：class0/class1/class2 三表同构，稀疏存储（无行=0 票）
# ============================================================

def create_question_db(question_id: str, path: str = None) -> str:
    """创建题目库。path 支持临时路径（出题先写 .tmp，自检通过后原子改名）。"""
    db_path = path or get_question_db_path(question_id)
    _ensure_parent(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    for table in TICKET_TABLES:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                train_num TEXT NOT NULL,
                from_station_id TEXT NOT NULL,
                to_station_id TEXT NOT NULL,
                tickets INTEGER NOT NULL CHECK (tickets >= 0 AND tickets <= 1000),
                UNIQUE(train_num, from_station_id, to_station_id)
            )
        """)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_train_num ON {table}(train_num)")
    conn.commit()
    conn.close()
    return db_path


def update_ticket(conn: sqlite3.Connection, train_num: str,
                  from_station_id: str, to_station_id: str,
                  seat_type: str, tickets: int):
    """更新站对余票：>0 写入（INSERT OR REPLACE），==0 删除行（稀疏存储）"""
    if seat_type not in TICKET_TABLES:
        raise ValueError(f"无效的座位类型: {seat_type}")
    if not (0 <= tickets <= 1000):
        raise ValueError(f"余票数量超出范围: {tickets}")
    if tickets > 0:
        conn.execute(f"""
            INSERT OR REPLACE INTO {seat_type}
            (train_num, from_station_id, to_station_id, tickets) VALUES (?, ?, ?, ?)
        """, (train_num, from_station_id, to_station_id, tickets))
    else:
        conn.execute(f"""
            DELETE FROM {seat_type}
            WHERE train_num = ? AND from_station_id = ? AND to_station_id = ?
        """, (train_num, from_station_id, to_station_id))
    conn.commit()


def get_train_tickets_filtered(conn: sqlite3.Connection, train_num: str,
                               from_station_id: Optional[str] = None,
                               to_station_id: Optional[str] = None,
                               seat_types: Optional[List[str]] = None) -> Dict:
    """按席别/站对筛选车次余票：{seat: {"from|to": count}}（step4 工具查询用）"""
    result: Dict[str, Dict] = {}
    for table in (seat_types or TICKET_TABLES):
        if table not in TICKET_TABLES:
            continue
        sql = "SELECT from_station_id, to_station_id, tickets FROM " + table + " WHERE train_num = ?"
        params: list = [train_num]
        if from_station_id and to_station_id:
            sql += " AND from_station_id = ? AND to_station_id = ?"
            params += [from_station_id, to_station_id]
        elif from_station_id:
            sql += " AND from_station_id = ?"
            params.append(from_station_id)
        elif to_station_id:
            sql += " AND to_station_id = ?"
            params.append(to_station_id)
        data = {f"{r[0]}|{r[1]}": r[2] for r in conn.execute(sql, params).fetchall()}
        result[table] = data
    return result


# ============================================================
# 题目元数据（metadata.json + metadata_nl.json）
# 全部读写经进程级可重入锁串行化，配合"锁内读-改-写"，杜绝并发丢更新；
# 落盘一律 tmp + os.replace 原子写、utf-8-sig 读（兼容 BOM）。
# ============================================================

_METADATA_LOCK = threading.RLock()


def load_metadata() -> Dict:
    """读主元数据并自动合并 metadata_nl.json 的 nl_question 字段
    （仅当 qid 仍存在于主文件时生效，删除题目后 NL 残留无副作用）。"""
    with _METADATA_LOCK:
        p = _path("metadata")
        metadata = {}
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                metadata = json.load(f)
        for qid, entry in _metadata_nl_unlocked().items():
            nlq = (entry or {}).get("nl_question") if isinstance(entry, dict) else None
            if nlq and isinstance(metadata.get(qid), dict):
                metadata[qid]["nl_question"] = nlq
        return metadata


def save_metadata(metadata: Dict):
    """整体保存主元数据（锁内原子写）。传入的应是刚 load 的最新快照。"""
    with _METADATA_LOCK:
        _save_json_atomic(_path("metadata"), metadata)


def get_question_metadata(question_id: str) -> Dict:
    return load_metadata().get(question_id, {})


def update_question_metadata(question_id: str, **fields):
    """读-改-写更新单题元数据。常用字段见签名下方字段表；
    question_mode 存在时同步推导 type（存在性/选择性）。"""
    FIELD_KEYS = ("train_count", "trains", "source", "question_type", "question",
                  "interference", "interference_density", "segment_plans",
                  "people_count", "seat_type", "interference_mode", "ground_truth",
                  "start_station_id", "end_station_id", "criterion", "constraints")
    with _METADATA_LOCK:
        p = _path("metadata")
        metadata = {}
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                metadata = json.load(f)
        entry = metadata.get(question_id)
        if not isinstance(entry, dict):
            entry = {"train_count": len(fields.get("trains") or [])}
            metadata[question_id] = entry
        for k in FIELD_KEYS:
            if fields.get(k) is not None:
                entry[k] = fields[k]
        if fields.get("question_mode") is not None:
            entry["question_mode"] = fields["question_mode"]
            entry["type"] = "存在性" if fields["question_mode"] == "existence" else "选择性"
        elif fields.get("interference_mode") is not None:
            entry["type"] = "存在性" if fields["interference_mode"] == "interference" else "选择性"
        if fields.get("trains") is not None:   # trains 合并去重并回写 train_count
            merged = set(entry.get("trains") or [])
            merged.update(fields["trains"])
            entry["trains"] = sorted(merged)
            entry["train_count"] = len(entry["trains"])
        _save_json_atomic(p, metadata)


# ---- NL 独立产物：NL 过程不触碰主元数据；读取侧由 load_metadata 自动合并 ----

def _metadata_nl_unlocked() -> Dict:
    p = _PATHS.get("metadata_nl")
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def update_metadata_nl(updates: Dict[str, str]):
    """增量写入 NL 产物：{qid: 文本}。每题存【主元数据条目快照 + nl_question】，
    已有快照沿用（保留首轮字段）。锁内原子写。"""
    if not updates:
        return
    with _METADATA_LOCK:
        nl = _metadata_nl_unlocked()
        p = _path("metadata")
        main = {}
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                main = json.load(f)
        for qid, text in updates.items():
            if not text:
                continue
            entry = nl.get(qid)
            if not isinstance(entry, dict):
                entry = dict(main.get(qid)) if isinstance(main.get(qid), dict) else {}
            entry["nl_question"] = text
            nl[qid] = entry
        _save_json_atomic(_path("metadata_nl"), nl)


# ---- 流水线 state：metadata[qid]["state"] = ["<model>:tested|evaluated", ...]
#      只记成功（失败可重试）；成功后同模型不可重测（去重依据） ----

def get_question_model_state(entry: Dict, model: str) -> Optional[str]:
    for s in (entry.get("state") or []):
        if isinstance(s, str) and ":" in s:
            m, _, st = s.rpartition(":")
            if m == model:
                return st or ""
    return None


def get_question_tested_models(entry: Dict) -> List[str]:
    out = set()
    for s in (entry.get("state") or []):
        if isinstance(s, str) and ":" in s:
            out.add(s.rsplit(":", 1)[0])
    return sorted(out)


def set_question_model_state(question_id: str, model: str, state: str):
    """锁内读-改-写设置某模型状态项（幂等）"""
    with _METADATA_LOCK:
        p = _path("metadata")
        metadata = {}
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                metadata = json.load(f)
        entry = metadata.get(question_id)
        if not isinstance(entry, dict):
            return
        states = [s for s in (entry.get("state") or [])
                  if not (isinstance(s, str) and s.rsplit(":", 1)[0] == model)]
        states.append(f"{model}:{state}")
        entry["state"] = states
        _save_json_atomic(p, metadata)


def _save_json_atomic(path: str, data):
    """JSON 原子落盘：写 .tmp 后 os.replace，进程被杀不留截断文件"""
    _ensure_parent(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ============================================================
# 同车多号映射（same_trains.json，step1 生成 / 票价爬取消费）
# ============================================================

def same_train_map_path() -> str:
    return os.path.join(_path("data_dir"), "same_trains.json")


def load_same_train_map() -> Dict[str, List[str]]:
    """{train_num → [同组其他车次号]}；文件缺失返回 {}（各消费方安全降级）"""
    path = same_train_map_path()
    if not os.path.exists(path):
        return {}
    groups = json.load(open(path, "r", encoding="utf-8"))
    return {tn: [t for t in g["trains"] if t != tn]
            for g in groups for tn in g["trains"]}
