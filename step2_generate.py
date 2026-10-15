"""
step2_generate.py —— 出题：按分布表（1.xlsx）+ 站对表（2.xlsx）批量生成题目

复刻参考项目出题工作流（语义 1:1）：
- 0_ 存在性·无注入（唯一解）/ 1_ 存在性·干扰（票数严格<人数，唯一解）/
  2_ 选择性·随机票（0.5~1.5×人数，多解）
- 五题型：transfer 换乘 / short_buy 买短补长 / extra_front 前额外 / extra_rear 后额外 /
  mixed 混合（一次换乘 + 两段策略）
- 流程：候选车次重试 → 临时库写标答 → 全局干扰注入（排除标答占用站对）→
  标答自反校验 + 自检 → 原子改名落盘 → 写 metadata

用法：
    python step2_generate.py --railway-db data/railway.db --prices-db data/prices.db ^
        --question-dir question --distribution 1.xlsx --stations 2.xlsx ^
        --seat-weights 20,30,50 --interference-density 0.02 --random-tickets-density 0.02 ^
        --max-retries 40 [--receipt 失败回执.xlsx]
"""

import argparse
import os
import random
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import config
from common import database as db
from common.xlsxio import (parse_distribution_xlsx, parse_stations_xlsx,
                           generate_failure_report_xlsx, criterion_cn, behavior_cn)
from verifier import check_strategy_constraints   # 标答自反校验复用核查端同一实现


# ============================================================
# 异常体系（替代参考版的 HTTPException）
# ============================================================
class GenerateError(Exception):
    """出题失败（参数/数据问题）"""


class RetryTrainError(GenerateError):
    """内层找不到合法解（中间站/换乘车不足、自检不通过等）→ 换下一辆候选车重试"""


# 重试关键词兜底：非 RetryTrainError 但语义可重试的错误描述
RETRY_KEYWORDS = (
    "目标车次无合适的中间站", "未找到合适的换乘车次", "混合策略换乘",
    "出发站前无足够车站", "到达站后无足够车站", "标答与行为约束冲突", "自检未通过",
)

VALID_TYPES = ["transfer", "short_buy", "extra_front", "extra_rear", "mixed"]
VALID_CONSTRAINTS = {"no_transfer", "no_short_buy_extra"}
VALID_CRITERIA = {"comprehensive", "fastest", "cheapest", "depart_latest", "arrive_earliest"}
TRANSFER_MIN_GAP = 20   # 换乘衔接最短分钟（所有题型统一）


@dataclass
class GenerateParams:
    """单题出题参数。from/to 可传中文站名或电报码（自动解析）。"""
    from_station_id: str
    to_station_id: str
    question_type: Optional[str] = None   # 存在性必传；选择性按行为约束自动推导
    transfers: int = 0                    # 换乘数（仅 mixed）
    segment_plans: List[str] = field(default_factory=list)   # 段策略，长度=transfers+1
    custom_qid: str = ""                  # 题名主体（前缀系统自动加）；空则时间戳生成
    people_count: Optional[int] = None    # 缺省随机 3~6
    seat_type: str = "class2"
    criterion: str = "comprehensive"      # 仅选择性；单选
    constraints: List[str] = field(default_factory=list)     # 仅选择性；no_transfer 与 no_short_buy_extra 互斥


# ============================================================
# 基础查询辅助
# ============================================================

def _time_diff_minutes(t1: str, t2: str) -> int:
    """t2-t1 的分钟数（仅当日；t2 时钟早于 t1 返回负值=跨天）"""
    try:
        h1, m1 = map(int, t1.split(":"))
        h2, m2 = map(int, t2.split(":"))
        return (h2 * 60 + m2) - (h1 * 60 + m1)
    except (ValueError, AttributeError):
        return 0


def _transfer_candidates(rw: sqlite3.Connection, from_id: str, to_id: str) -> List[Dict]:
    """transfer/mixed 题型的候选车次 T：经过 A、不经过 B（防 T 直达逃逸）、A 后仍有站"""
    candidates = []
    for (tn,) in rw.execute(
            "SELECT DISTINCT train_num FROM train_stops WHERE station_id = ?", (from_id,)).fetchall():
        stops = db.get_train_stops(rw, tn)
        ids = [s["station_id"] for s in stops]
        if to_id in ids or from_id not in ids:
            continue
        i = ids.index(from_id)
        if i < len(stops) - 1:
            candidates.append({"train_num": tn, "depart_time": stops[i]["stop_time"],
                               "arrive_time": stops[-1]["stop_time"]})
    return candidates


def _city_key(station_name: str) -> str:
    """站名 → 城市标识（去方位后缀），用于排除与目的地同城的中转站"""
    for suf in ("东", "南", "西", "北", "站"):
        if station_name.endswith(suf) and len(station_name) > len(suf) + 1:
            return station_name[:-len(suf)]
    return station_name


def find_transfer_solutions(rw: sqlite3.Connection, excluded_train: str,
                            mid_refs: List[Tuple[str, str]], to_id: str,
                            min_gap: int = TRANSFER_MIN_GAP) -> List[Dict]:
    """核心一：一次 SQL 枚举全部可行换乘 (M, U)。

    mid_refs: [(中间站, 上一程到达该站的参考时间)]。U 须经过 M 且 M 在 to_id 之前、
    非排除车次、参考时间 + min_gap ≤ U 从 M 发车。返回含 gap 的方案列表。
    """
    if not mid_refs:
        return []
    mid_ids = [m for m, _ in mid_refs]
    ref_map = dict(mid_refs)
    ph = ",".join("?" * len(mid_ids))
    rows = rw.execute(f"""
        SELECT u_m.station_id, u_m.stop_time, u_b.stop_time, u_m.train_num
        FROM train_stops u_m
        JOIN train_stops u_b ON u_b.train_num = u_m.train_num AND u_b.station_id = ?
        WHERE u_m.station_id IN ({ph}) AND u_m.stop_no < u_b.stop_no AND u_m.train_num != ?
        ORDER BY u_m.stop_time
    """, [to_id] + mid_ids + [excluded_train]).fetchall()

    solutions = []
    for mid_id, u_dep, u_arr, u_train in rows:
        ref = ref_map.get(mid_id)
        if ref and u_dep and _time_diff_minutes(ref, u_dep) >= min_gap:
            solutions.append({"mid_id": mid_id, "train_num": u_train, "u_m_depart": u_dep,
                              "u_b_arrive": u_arr, "ref_time": ref,
                              "gap": _time_diff_minutes(ref, u_dep)})
    return solutions


def _find_transfer_trains(rw, target_train, mid_id, to_id, mid_stop_time,
                          min_gap=TRANSFER_MIN_GAP) -> List[Dict]:
    """单中间站包装：M→B 的可选车次（mixed 段间换乘用）"""
    return [{"train_num": s["train_num"], "mid_depart": s["u_m_depart"],
             "arrive_time": s["u_b_arrive"]}
            for s in find_transfer_solutions(rw, target_train, [(mid_id, mid_stop_time)],
                                             to_id, min_gap)]


def random_solution_tickets(people_count: int, rng: random.Random) -> int:
    """标答票数：1~1.5×人数随机（保证 ≥ 人数；人数=1 退化为 1）"""
    return rng.randint(people_count, max(people_count, int(people_count * 1.5)))


# ============================================================
# 标答段构造
# ============================================================

def _purchase_segment(train_num: str, buy_from: str, buy_to: str, ride_from: str,
                      ride_to: str, names: Dict[str, str], tickets: int,
                      seat_type: str, strategy: str = None) -> Dict:
    """组装一个购票段（购买区间 + 实际乘坐区间；电报码与站名并存）"""
    seg = {
        "train_num": train_num,
        "from": names.get(buy_from, buy_from), "to": names.get(buy_to, buy_to),
        "from_station_id": buy_from, "to_station_id": buy_to,
        "ride_from": names.get(ride_from, ride_from), "ride_to": names.get(ride_to, ride_to),
        "ride_from_station_id": ride_from, "ride_to_station_id": ride_to,
        "tickets": tickets, "seat_type": seat_type,
    }
    if strategy:
        seg["strategy"] = strategy
    return seg


def _apply_strategy(q_conn, rw, strategy: str, train_num: str, stops: List[Dict],
                    from_idx: int, to_idx: int, people_count: int,
                    seat_type: str, rng: random.Random,
                    names: Dict[str, str] = None, seg_label: str = "") -> Dict:
    """在指定车次的 [from_idx, to_idx] 区间上应用单段购票策略，写库并返回 segment。

    - direct：直接买全程
    - short_buy：买 A→M（M 为中间随机站），实际乘坐到 to
    - extra_front：往前 1~3 站买 Aₖ→to，实际 from 上车
    - extra_rear：往后 1~3 站买 from→Dₖ，实际在 to 提前下车
    """
    ids = [s["station_id"] for s in stops]
    names = names or {s["station_id"]: s["station_name"] for s in stops}
    f_id, t_id = ids[from_idx], ids[to_idx]

    if strategy == "direct":
        tk = random_solution_tickets(people_count, rng)
        db.update_ticket(q_conn, train_num, f_id, t_id, seat_type, tk)
        return _purchase_segment(train_num, f_id, t_id, f_id, t_id, names, tk, seat_type, "direct")

    if strategy == "short_buy":
        mids = list(range(from_idx + 1, to_idx))
        if not mids:
            raise RetryTrainError(f"{seg_label}买短补长无中间站")
        mid_id = ids[rng.choice(mids)]
        tk = random_solution_tickets(people_count, rng)
        db.update_ticket(q_conn, train_num, f_id, mid_id, seat_type, tk)
        return _purchase_segment(train_num, f_id, mid_id, f_id, t_id, names, tk, seat_type, "short_buy")

    if strategy == "extra_front":
        max_extra = min(from_idx, 3)
        if max_extra < 1:
            raise RetryTrainError(f"{seg_label}前额外无足够车站")
        extra_from = ids[from_idx - rng.randint(1, max_extra)]
        tk = random_solution_tickets(people_count, rng)
        db.update_ticket(q_conn, train_num, extra_from, t_id, seat_type, tk)
        return _purchase_segment(train_num, extra_from, t_id, f_id, t_id, names, tk, seat_type, "extra_front")

    if strategy == "extra_rear":
        max_extra = min(len(ids) - 1 - to_idx, 3)
        if max_extra < 1:
            raise RetryTrainError(f"{seg_label}后额外无足够车站")
        extra_to = ids[to_idx + rng.randint(1, max_extra)]
        tk = random_solution_tickets(people_count, rng)
        db.update_ticket(q_conn, train_num, f_id, extra_to, seat_type, tk)
        return _purchase_segment(train_num, f_id, extra_to, f_id, t_id, names, tk, seat_type, "extra_rear")

    raise RetryTrainError(f"未知段策略: {strategy}")


def write_legal_solution(q_conn, rw, question_type: str, train_num: str, stops: List[Dict],
                         from_idx: int, to_idx: int, segment_plans: List[str],
                         transfer_dest: Optional[str], to_id: str, seat_type: str,
                         people_count: int, rng: random.Random,
                         route_hint: Optional[Dict] = None) -> Dict:
    """写标答（正数票）。transfer 用核心一均匀随机挑 (M, U)；mixed 按段策略逐段换乘。
    to_id = 用户真实目的地（mixed 的路径终点；to_idx 是目标车 T 的末站，二者不同——
    T 故意不经过 B，其末站并非目的地）。返回 {"segments": [...]}。
    任何"此车不合适"都抛 RetryTrainError 换下一辆。

    route_hint（配对重放）：非空时忽略上述全部随机逻辑，按 1_ 成功题记录的路线结构
    （各段车次/购买区间/乘坐区间/票数）机械重建标答——0_ 与 1_ 同解的保证。"""
    ids = [s["station_id"] for s in stops]
    names = {s["station_id"]: s["station_name"] for s in stops}
    segments: List[Dict] = []

    if route_hint:
        # 重放模式：同库同数据下必然可行（结构来自本进程刚生成成功的 1_ 题），
        # 异常属于程序错误，直接 GenerateError（重放没有"换下一辆"的概念）
        for leg in route_hint["legs"]:
            cur_train = str(leg["train_num"])
            cur_stops = db.get_train_stops(rw, cur_train)
            cur_ids = [s["station_id"] for s in cur_stops]
            leg_names = {s["station_id"]: s["station_name"] for s in cur_stops}
            buy_from, buy_to = str(leg["buy_from"]), str(leg["buy_to"])
            ride_from, ride_to = str(leg["ride_from"]), str(leg["ride_to"])
            tk = int(leg["tickets"])
            if (buy_from not in cur_ids or buy_to not in cur_ids
                    or cur_ids.index(buy_from) >= cur_ids.index(buy_to)
                    or ride_from not in cur_ids or ride_to not in cur_ids
                    or cur_ids.index(ride_from) >= cur_ids.index(ride_to)):
                raise GenerateError(f"标答路线重放失败：车次 {cur_train} 上 "
                                    f"{buy_from}→{buy_to}（乘 {ride_from}→{ride_to}）不可重建")
            db.update_ticket(q_conn, cur_train, buy_from, buy_to, seat_type, tk)
            segments.append(_purchase_segment(cur_train, buy_from, buy_to, ride_from, ride_to,
                                              leg_names, tk, seat_type, leg.get("strategy")))
        return {"segments": segments}

    if question_type == "transfer":
        actual_dest = transfer_dest or ids[to_idx]
        # 过滤与目的地同城的中转站
        dest_name = (rw.execute("SELECT station_name FROM stations WHERE station_id = ?",
                                (actual_dest,)).fetchone() or [actual_dest])[0]
        dest_city = _city_key(dest_name)
        mids = [i for i in range(from_idx + 1, to_idx)
                if _city_key(names.get(ids[i], "")) != dest_city]
        if not mids:
            raise RetryTrainError("目标车次无合适的中间站")
        sols = find_transfer_solutions(rw, train_num,
                                       [(ids[i], stops[i]["stop_time"]) for i in mids],
                                       actual_dest)
        # 与 mixed 同规则：换乘车一律排除经过出发站 A 的车，
        # 保证「换乘车不经过 A」全局不变量（杜绝"只坐 U 从 A 直达 B"类逃逸）
        sols = [s for s in sols
                if not any(st["station_id"] == ids[from_idx]
                           for st in db.get_train_stops(rw, s["train_num"]))]
        if not sols:
            raise RetryTrainError(f"未找到合适的换乘车次到 {actual_dest}（已枚举全部中间站）")
        chosen = rng.choice(sols)
        tk1, tk2 = random_solution_tickets(people_count, rng), random_solution_tickets(people_count, rng)
        u_names = {s["station_id"]: s["station_name"] for s in db.get_train_stops(rw, chosen["train_num"])}
        # 段1：T 乘 A→M；段2：U 乘 M→实际目的地
        segments.append(_purchase_segment(train_num, ids[from_idx], chosen["mid_id"],
                                          ids[from_idx], chosen["mid_id"], names, tk1, seat_type))
        segments.append(_purchase_segment(chosen["train_num"], chosen["mid_id"], actual_dest,
                                          chosen["mid_id"], actual_dest, u_names, tk2, seat_type))
        db.update_ticket(q_conn, train_num, ids[from_idx], chosen["mid_id"], seat_type, tk1)
        db.update_ticket(q_conn, chosen["train_num"], chosen["mid_id"], actual_dest, seat_type, tk2)

    elif question_type in ("short_buy", "extra_front", "extra_rear"):
        segments.append(_apply_strategy(q_conn, rw, question_type, train_num, stops,
                                        from_idx, to_idx, people_count, seat_type, rng, names))

    elif question_type == "mixed" and segment_plans:
        transfers = len(segment_plans) - 1
        mids = list(range(from_idx + 1, to_idx))
        if len(mids) < transfers:
            raise RetryTrainError(f"混合题型需要至少 {transfers} 个中间站，当前只有 {len(mids)}")
        selected = sorted(rng.sample(mids, transfers)) if transfers else []
        path = [ids[from_idx]] + [ids[i] for i in selected] + [to_id]   # 终点=用户真实目的地

        cur_train, cur_stops = train_num, stops
        arrival_at_seg_start = None   # 上一段车到达本段起点的时间（换乘衔接基准）
        for si, strategy in enumerate(segment_plans):
            seg_from, seg_to = path[si], path[si + 1]
            if si > 0:
                cands = _find_transfer_trains(rw, train_num, seg_from, seg_to, arrival_at_seg_start)
                # 一律排除经过出发站 A 的换乘车：否则「只坐 U 从 A 直达 B」成为第二合法解，
                # 破坏存在性题唯一解前提（模型给出合法解反被标答比对误判）
                cands = [c for c in cands
                         if not any(s["station_id"] == ids[from_idx]
                                    for s in db.get_train_stops(rw, c["train_num"]))]
                if strategy == "short_buy" and si < transfers:
                    # 非末段买短补长：排除经过最终站 B 的车（防跳过后续换乘直达终点）
                    cands = [c for c in cands
                             if not any(s["station_id"] == to_id
                                        for s in db.get_train_stops(rw, c["train_num"]))]
                    if not cands:
                        raise RetryTrainError(f"段 {si} 买短补长无可用的换乘车次（候选均经过终点站）")
                if not cands:
                    raise RetryTrainError(f"未找到从 {seg_from} 到 {seg_to} 的换乘车次")
                if si < transfers:   # 后续还有换乘：选中车必须在本段终点有有效到达时间
                    cands = [c for c in cands if re.fullmatch(r"\d{2}:\d{2}", c.get("arrive_time") or "")]
                    if not cands:
                        raise RetryTrainError(f"未找到有有效到达时间的换乘车次从 {seg_from} 到 {seg_to}")
                cur_train = rng.choice(cands)["train_num"]
                cur_stops = db.get_train_stops(rw, cur_train)
                arrival_at_seg_start = next(s["stop_time"] for s in cur_stops
                                            if s["station_id"] == seg_to)
            else:
                arrival_at_seg_start = next(s["stop_time"] for s in stops if s["station_id"] == seg_to)

            cur_ids = [s["station_id"] for s in cur_stops]
            try:
                fi, ti = cur_ids.index(seg_from), cur_ids.index(seg_to)
            except ValueError:
                raise RetryTrainError(f"车次 {cur_train} 不经过 {seg_from} 或 {seg_to}")
            segments.append(_apply_strategy(q_conn, rw, strategy, cur_train, cur_stops,
                                            fi, ti, people_count, seat_type, rng,
                                            seg_label=f"段 {si} "))
    else:
        raise GenerateError(f"无效的题型: {question_type}")

    return {"segments": segments}


# ============================================================
# 干扰 / 随机票注入
# ============================================================

_STOPS_CACHE: Optional[Dict[str, List[Dict]]] = None
_STOPS_CACHE_MT: Optional[float] = None


def _all_stops_cached(rw: sqlite3.Connection) -> Dict[str, List[Dict]]:
    """全库经停站内存缓存（约 6 万条，首次 ~0.8s，此后毫秒级构建候选池）；
    railway.db mtime 变化自动失效（重爬数据后无需重启）。"""
    global _STOPS_CACHE, _STOPS_CACHE_MT
    try:
        mt = os.path.getmtime(db._path("railway_db"))
    except OSError:
        mt = None
    if _STOPS_CACHE is None or mt != _STOPS_CACHE_MT:
        cache: Dict[str, List[Dict]] = {}
        for tn, no, sid, name, tm in rw.execute(
                "SELECT train_num, stop_no, station_id, station_name, stop_time "
                "FROM train_stops ORDER BY train_num, stop_no"):
            cache.setdefault(tn, []).append({"stop_no": no, "station_id": sid,
                                             "station_name": name, "stop_time": tm})
        _STOPS_CACHE, _STOPS_CACHE_MT = cache, mt
    return _STOPS_CACHE


def add_interference_all_trains(q_conn, rw, solution_occupied: set, density: float,
                                interference_mode: str, people_count: int,
                                rng: random.Random, block_pairs: set = None,
                                guard_from: str = None, guard_to: str = None):
    """全局池注入：候选 = 全部车次全部 i<j 站对（一次 shuffle 取前 密度×池大小 个）。

    - 标答占用 (车次,购买起,购买讫) 一律跳过（含换乘车上的段，保证与标答完全错开）
    - block_pairs：全局禁注站对（随机票题的直达 (A,B)）
    - interference：randint(1, 人数-1) 严格不足 → 唯一解（1 人时退化为不注入）
    - random_tickets：randint(0.5×, 1.5×人数) → 多解（部分够票、部分差一张）
    - guard_from/to：随机票 transfer/mixed 题防"买短补长直达 B"逃逸——对先 A 后 B 的车
      跳过 (A, X<B) 站对
    """
    if interference_mode == "interference" and people_count <= 1:
        return   # 干扰需票数<1 即 0 张，等价于不注入，保持唯一解
    block_pairs = block_pairs or set()
    pool: List[tuple] = []
    for tn, stops in _all_stops_cached(rw).items():
        if len(stops) < 2:
            continue
        ids = [s["station_id"] for s in stops]
        guard = None
        if guard_from and guard_to and guard_from in ids and guard_to in ids:
            ia, ib = ids.index(guard_from), ids.index(guard_to)
            if ia < ib:
                guard = (ia, ib)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                f, t = ids[i], ids[j]
                if (tn, f, t) in solution_occupied or (f, t) in block_pairs:
                    continue
                if guard is not None and i == guard[0] and j < guard[1]:
                    continue
                pool.append((tn, f, t))
    rng.shuffle(pool)
    for tn, f, t in pool[:int(len(pool) * density)]:
        seat = rng.choice(db.TICKET_TABLES)
        if interference_mode == "interference":
            tk = rng.randint(1, max(1, people_count - 1))
        else:
            lo = max(1, int(people_count * 0.5))
            tk = rng.randint(lo, max(lo, int(people_count * 1.5)))
        q_conn.execute(f"INSERT OR REPLACE INTO {seat} (train_num, from_station_id, "
                       "to_station_id, tickets) VALUES (?, ?, ?, ?)", (tn, f, t, tk))
    q_conn.commit()


def _clear_direct_route(q_conn, train_num: str, from_id: str, to_id: str):
    """删除直达区间余票（随机票题防直达逃逸）"""
    for table in db.TICKET_TABLES:
        q_conn.execute(f"DELETE FROM {table} WHERE train_num = ? AND from_station_id = ? "
                       "AND to_station_id = ?", (train_num, from_id, to_id))
    q_conn.commit()


# ============================================================
# 自检（落盘前兜底断言）
# ============================================================

def self_check(q_conn, segments: List[Dict], people_count: int,
               interference_mode: str, solution_occupied: set) -> Optional[str]:
    """返回 None=通过。① 标答每段库中票数==标注且≥人数；② 干扰题唯一解断言：
    除标答占用站对外全库票数严格 < 人数。"""
    try:
        for seg in segments:
            seat = seg.get("seat_type", "")
            if seat not in db.TICKET_TABLES:
                continue
            row = q_conn.execute(
                f"SELECT tickets FROM {seat} WHERE train_num=? AND from_station_id=? AND to_station_id=?",
                (seg["train_num"], seg["from_station_id"], seg["to_station_id"])).fetchone()
            actual = row[0] if row else None
            if actual != seg.get("tickets"):
                return (f"标答段 {seg['train_num']} {seg['from_station_id']}→{seg['to_station_id']} "
                        f"票数与标注不符（标注 {seg.get('tickets')} 实际 {actual}）")
            if actual is None or actual < people_count:
                return f"标答段 {seg['train_num']} 票数 {actual} 不足需求人数 {people_count}"
        if interference_mode == "interference":
            for table in db.TICKET_TABLES:
                for tn, f, t, tk in q_conn.execute(
                        f"SELECT train_num, from_station_id, to_station_id, tickets FROM {table}"):
                    if (tn, f, t) not in solution_occupied and tk >= people_count:
                        return f"干扰题唯一解被破坏：{tn} {f}→{t}（{table}）有 {tk} 张票（≥人数 {people_count}）"
        return None
    except sqlite3.Error as e:
        return f"自检查询异常: {e}"


# ============================================================
# 出题主函数
# ============================================================

def generate_question(params: GenerateParams, prefix: str, mode: str,
                      interference_mode: str, density: float,
                      rng: random.Random,
                      route_hint: Optional[Dict] = None) -> Tuple[str, Dict]:
    """生成一道题并直接落盘。返回 (question_id, summary)；重试耗尽抛 GenerateError。

    prefix: "0_"/"1_"（存在性）/"2_"（选择性）；mode: existence/selective
    interference_mode: none / interference / random_tickets
    route_hint: 配对重放的路线结构（0_ 复用 1_ 的解，保证同解；非配对题不传）
    """
    # ---- 选择性题题型自动推导（保证有解）：禁换乘 → 买短补长，否则换乘 ----
    if mode == "selective":
        params.question_type = "short_buy" if "no_transfer" in (params.constraints or []) else "transfer"
    elif params.question_type is None:
        raise GenerateError("存在性题必须指定题型")
    if params.question_type not in VALID_TYPES:
        raise GenerateError(f"无效的题型: {params.question_type}")

    if params.people_count is None:
        params.people_count = rng.randint(3, 6)
    if not (1 <= params.people_count <= config.QUESTION_CONFIG["max_people_count"]):
        raise GenerateError(f"人数超出范围: {params.people_count}")
    if params.seat_type not in db.TICKET_TABLES:
        raise GenerateError(f"无效的座位类型: {params.seat_type}")
    if params.question_type == "mixed":
        if params.transfers < 1:
            raise GenerateError("混合题型换乘数至少为 1")
        if len(params.segment_plans) != params.transfers + 1:
            raise GenerateError(f"段策略数需等于换乘数+1（{params.transfers + 1}）")

    # ---- 行为约束/评判标准校验（仅选择性） ----
    constraints = params.constraints or []
    if [c for c in constraints if c not in VALID_CONSTRAINTS]:
        raise GenerateError(f"无效的行为约束: {constraints}")
    if len(constraints) != len(set(constraints)):
        raise GenerateError("行为约束存在重复项")
    if "no_transfer" in constraints and "no_short_buy_extra" in constraints:
        raise GenerateError("行为约束「不允许换乘」与「不允许买短补长与额外购买」互斥，"
                            "同选会使题面自相矛盾、无任何合法解")
    if constraints and mode != "selective":
        raise GenerateError("行为约束仅选择性题支持")
    if params.criterion not in VALID_CRITERIA:
        raise GenerateError(f"无效的评判标准: {params.criterion}")
    if mode != "selective" and params.criterion != "comprehensive":
        raise GenerateError("评判标准仅选择性题支持")

    rw = db.get_railway_conn()
    try:
        from_id = db.resolve_station_name_or_id(rw, params.from_station_id)
        to_id = db.resolve_station_name_or_id(rw, params.to_station_id)
        if not db.validate_station_exists(rw, from_id):
            raise GenerateError(f"出发站 {from_id} 不存在")
        if not db.validate_station_exists(rw, to_id):
            raise GenerateError(f"到达站 {to_id} 不存在")

        # 候选车次：transfer/mixed 用"不经过 B"的候选集，其余为直达车次；
        # 配对重放（route_hint）时锁定 1_ 成功题的目标车次，不再候选/随机
        if route_hint:
            routes = [{"train_num": route_hint["target_train"]}]
        elif params.question_type in ("transfer", "mixed"):
            routes = _transfer_candidates(rw, from_id, to_id)
        else:
            routes = db.get_routes_between(rw, from_id, to_id)
        if not routes:
            raise GenerateError(f"未找到从 {from_id} 到 {to_id} 的车次")

        rng.shuffle(routes)
        existing_metadata = db.read_metadata_file(db._path("metadata"))   # 题号查重基准=metadata.json（读一次）

        last_error = None
        for attempt in range(min(len(routes), 15)):
            target_train = routes[attempt]["train_num"]
            stops = db.get_train_stops(rw, target_train)
            stop_ids = [s["station_id"] for s in stops]
            from_idx = stop_ids.index(from_id)
            if params.question_type in ("transfer", "mixed"):
                # T 不经过 B：to_idx 取末站，实际终点由换乘车到达
                to_idx, cur_to_id = len(stops) - 1, (to_id if params.question_type == "mixed" else stops[-1]["station_id"])
                cur_from_id, cur_from_idx = from_id, from_idx
                cur_to_idx = to_idx
            else:
                to_idx = stop_ids.index(to_id)
                if from_idx > to_idx:   # 反向车次：对称翻转区间
                    cur_from_id, cur_to_id, cur_from_idx, cur_to_idx = to_id, from_id, to_idx, from_idx
                else:
                    cur_from_id, cur_to_id, cur_from_idx, cur_to_idx = from_id, to_id, from_idx, to_idx

            # 预检：中间站数量 / extra 前后余量
            middle = cur_to_idx - cur_from_idx - 1
            if params.question_type not in ("extra_front", "extra_rear") and middle < 1:
                continue
            if params.question_type == "mixed" and middle < params.transfers:
                last_error = f"中间站不足：需要 {params.transfers} 个，最多 {middle} 个"
                continue
            if params.question_type == "extra_front" and cur_from_idx < 1:
                last_error = "出发站前无足够车站做前额外"
                continue
            if params.question_type == "extra_rear" and cur_to_idx >= len(stops) - 1:
                last_error = "到达站后无足够车站做后额外"
                continue

            # ---- 题名（前缀系统自动加）+ 查重 ----
            if params.custom_qid:
                question_id = prefix + params.custom_qid
            else:
                question_id = f"{prefix}{datetime.now().strftime('%Y%m%d_%H%M%S')}_{attempt}"
            if os.path.exists(db.get_question_db_path(question_id)) or question_id in existing_metadata:
                last_error = f"题名 {question_id} 已存在"
                continue

            # ---- 临时库 → 标答 → 注入 → 校验 → 原子落盘 ----
            db_path = db.get_question_db_path(question_id)
            tmp_path = db_path + ".tmp"
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            db.create_question_db(question_id, path=tmp_path)
            q_conn = sqlite3.connect(tmp_path)
            q_conn.execute("PRAGMA foreign_keys = ON")
            try:
                transfer_dest = to_id if params.question_type == "transfer" else None
                segments = write_legal_solution(
                    q_conn, rw, params.question_type, target_train, stops,
                    cur_from_idx, cur_to_idx, params.segment_plans, transfer_dest,
                    to_id, params.seat_type, params.people_count, rng,
                    route_hint=route_hint)["segments"]

                solution_occupied = {(s["train_num"], s["from_station_id"], s["to_station_id"])
                                     for s in segments}

                if interference_mode != "none":
                    if interference_mode == "interference":
                        block, gf, gt = set(), None, None   # 干扰无逃逸可能，不设禁注
                    else:
                        block = {(from_id, to_id)}          # 随机票：禁直达 (A,B)
                        gf, gt = ((from_id, to_id)
                                  if params.question_type in ("transfer", "mixed") else (None, None))
                    add_interference_all_trains(q_conn, rw, solution_occupied, density,
                                                interference_mode, params.people_count, rng,
                                                block_pairs=block, guard_from=gf, guard_to=gt)
                    if interference_mode == "random_tickets":
                        _clear_direct_route(q_conn, target_train, from_id, to_id)
                q_conn.commit()

                # 标答自反校验：带行为约束时标答自身必须零违规
                if constraints:
                    violations = check_strategy_constraints(segments, constraints)
                    if violations:
                        raise RetryTrainError(f"标答与行为约束冲突: {violations[0].get('detail', '')}")

                err = self_check(q_conn, segments, params.people_count, interference_mode,
                                 solution_occupied)
                if err:
                    raise RetryTrainError(f"自检未通过: {err}")

                q_conn.close()
                os.replace(tmp_path, db_path)   # 原子落盘
            except RetryTrainError as e:
                # 内层判定此车不合适 → 清理临时库，换下一辆候选车重试
                q_conn.close()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                last_error = str(e)
                continue
            except GenerateError as e:
                q_conn.close()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                if any(kw in str(e) for kw in RETRY_KEYWORDS):   # 关键词兜底：语义可重试
                    last_error = str(e)
                    continue
                raise   # 参数/数据类错误：直接失败
            except Exception as e:
                q_conn.close()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                last_error = f"出题过程异常: {e}"
                continue

            # ---- 落盘成功：写 metadata ----
            names = {s["station_id"]: s["station_name"] for s in stops}
            from_name = names.get(from_id, from_id)
            dest = rw.execute("SELECT station_name FROM stations WHERE station_id = ?",
                              (to_id,)).fetchone()
            dest_name = dest[0] if dest else to_id
            question_str = f"{from_name}到{dest_name}"
            trains_list = sorted({s["train_num"] for s in segments})
            db.update_question_metadata(
                question_id=question_id,
                train_count=len(trains_list),
                trains=trains_list,
                source="auto",
                question_type=params.question_type,
                question=question_str,
                segment_plans=params.segment_plans,
                interference=interference_mode != "none",
                interference_mode=interference_mode if interference_mode != "none" else None,
                question_mode=mode,
                people_count=params.people_count,
                seat_type=params.seat_type,
                start_station_id=from_id,
                end_station_id=to_id,
                # 存在性题不写评判标准：题面不应带「综合考虑」（诱导模型偏离唯一解），仅选择性题有
                criterion=params.criterion if mode == "selective" else "",
                ground_truth=segments,
                interference_density=density if interference_mode != "none" else None,
                constraints=constraints or None,
            )
            return question_id, {
                "question": question_str,
                "question_type": params.question_type,
                "target_train_num": target_train,
                "solution_segments": segments,
                "criterion": params.criterion if mode == "selective" else "",
                "constraints": constraints,
            }

        raise GenerateError(f"所有车次重试后仍无法生成题目: {last_error or '未知错误'}")
    finally:
        rw.close()


# ============================================================
# 批量出题
# ============================================================

def _route_hint_from(summary: Dict) -> Dict:
    """从 1_ 成功题的出题摘要提取路线结构（供配对 0_ 重放，保证 0_/1_ 同解）：
    目标车次 + 各段（车次/策略/购买区间/乘坐区间/票数）。"""
    return {
        "target_train": summary["target_train_num"],
        "legs": [{"train_num": s["train_num"],
                  "strategy": s.get("strategy"),
                  "buy_from": s["from_station_id"], "buy_to": s["to_station_id"],
                  "ride_from": s["ride_from_station_id"], "ride_to": s["ride_to_station_id"],
                  "tickets": s["tickets"]}
                 for s in summary["solution_segments"]],
    }


def run_batch(distribution: List[Dict], selective_rows: List[Dict], stations: List[List[str]],
              seat_weights: Dict[str, float], interference_density: float,
              random_tickets_density: float, max_retries: int) -> Dict:
    """按分布批量出题：每题固定参数重试 ≤ max_retries，每次重试重随机
    站对/方向/人数/座位（换站对提高成功率）。返回 report（与 1.xlsx 同构的失败统计）。"""

    def log(msg: str, level: str = "info"):
        mark = {"info": "·", "success": "✔", "error": "✘", "warn": "⚠"}[level]
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {mark} {msg}")

    exists_total = sum((r.get("has_interference") or 0) + (r.get("no_interference") or 0)
                       for r in distribution)
    sel_total = sum(r.get("count") or 0 for r in selective_rows)
    log(f"批量出题开始：共 {exists_total + sel_total} 题（存在性 {exists_total} + 选择性 {sel_total}），"
        f"每题重试上限 {max_retries}，干扰密度 {interference_density:.1%}，随机票密度 {random_tickets_density:.1%}")

    details: List[Dict] = []
    exists_fail = [{"has_interference": 0, "no_interference": 0} for _ in distribution]
    seq = 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def generate_one(qtype, transfers, plans, criterion, constraints_, prefix, imode, density,
                     custom_qid_base: str = None, reuse: dict = None, route_hint: dict = None):
        """重试 ≤ max_retries，每次重随机站对/方向/人数/座位。
        成功返回 (qid, 尝试数, None, 首次序号, 随机参数, 路线结构)，
        失败返回 (None, max_retries, 错误, 首次序号, None, None)。
        custom_qid_base：配对题复用的基础号（同 row 的 0_ 复用 1_ 的），否则按 seq 递增生成。
        reuse：配对题复用的随机参数（站对/方向/人数/座位）——0_ 与 1_ 题面一致才能被
        step3 按「基础题号+内容」配对共享文案（对比实验前提）；不传则独立随机。
        route_hint：配对题重放的路线结构（仅 0_ 传入，来自 1_ 成功题）——保证 0_/1_ 同解。"""
        nonlocal seq
        last_err = ""
        first_seq = None
        for attempt in range(1, max_retries + 1):
            if reuse:
                pair, a, b = reuse["pair"], reuse["a"], reuse["b"]
                people = reuse["people"]
                seat = reuse["seat"]
            else:
                pair = random.choice(stations)
                a, b = (pair[0], pair[1]) if random.random() < 0.5 else (pair[1], pair[0])
                people = random.randint(3, 6)
                seat = random.choices(
                    ["class0", "class1", "class2"],
                    weights=[seat_weights.get("class0", 0), seat_weights.get("class1", 0),
                             seat_weights.get("class2", 0)])[0]
            seq += 1
            if first_seq is None:
                first_seq = seq   # 首次尝试的序号（失败回执/日志用它，对齐参考 first_sq）
            params = GenerateParams(
                question_type=qtype,
                from_station_id=a,
                to_station_id=b,
                transfers=transfers,
                segment_plans=plans,
                custom_qid=custom_qid_base or f"B{timestamp}_{seq:04d}",
                people_count=people,
                seat_type=seat,
                criterion=criterion,
                constraints=constraints_,
            )
            try:
                qid, summary = generate_question(params, prefix,
                                                 "existence" if prefix != "2_" else "selective",
                                                 imode, density, rng=random.Random(),
                                                 route_hint=route_hint)
                used = None if reuse else {"pair": pair, "a": a, "b": b,
                                           "people": people, "seat": seat}
                # 1_ 成功题的路线结构：供配对 0_ 重放（保证 0_/1_ 同解，题库仅差干扰）
                hint_out = _route_hint_from(summary) if (prefix == "1_" and qid) else None
                return qid, attempt, None, first_seq, used, hint_out
            except (GenerateError, db.PathsNotConfigured) as e:
                last_err = str(e)
            except Exception as e:
                last_err = str(e)
        return None, max_retries, last_err, first_seq, None, None

    # ---- 存在性：1_ 干扰 / 0_ 无注入（同 row 配对：共享基础号+随机参数保证题面一致，
    #      0_ 另复用 1_ 的路线结构重放标答 → 同解，两题题库仅差干扰注入） ----
    for idx, row in enumerate(distribution):
        label = f"{row.get('category', '')}/{row.get('name', '')}"
        ones_base = None   # 同 row 1_ 成功后的基础号（B..._NNNN），最近的 0_ 复用以配对
        ones_rand = None   # 同 row 1_ 成功那次的随机参数（站对/方向/人数/座位）
        ones_hint = None   # 同 row 1_ 成功那次的路线结构（供 0_ 重放，保证同解）
        for key, prefix, imode, tag in (("has_interference", "1_", "interference", "干扰"),
                                        ("no_interference", "0_", "none", "无干扰")):
            for _ in range(int(row.get(key) or 0)):
                pair_base = reuse = hint = None
                if prefix == "0_" and ones_base:
                    # 用掉即焚：基础号防重号冲突，随机参数保证 0_/1_ 题面一致，
                    # 路线结构重放保证同解（0_ 题库 = 1_ 题库 − 干扰注入）
                    pair_base, ones_base = ones_base, None
                    reuse, ones_rand = ones_rand, None
                    hint, ones_hint = ones_hint, None
                qid, tries, err, sq, rand_used, hint_out = generate_one(
                    row["question_type"], int(row.get("transfers") or 0),
                    row.get("segment_plans") or [], "comprehensive",
                    [], prefix, imode, interference_density,
                    custom_qid_base=pair_base, reuse=reuse, route_hint=hint)
                details.append({"question_id": qid or f"{prefix}B{timestamp}_{sq:04d}",
                                "row": f"{label}/{tag}", "attempts": tries,
                                "ok": qid is not None, "error": err})
                if qid is None:
                    exists_fail[idx][key] += 1
                    log(f"[{sq:04d}] {prefix} 存在性（{tag}）{label} 失败（重试 {tries} 次）：{err}", "error")
                else:
                    if prefix == "1_":
                        ones_base = qid[len(prefix):]   # "1_B..._0005" → "B..._0005"
                        ones_rand = rand_used
                        ones_hint = hint_out
                    log(f"[{sq:04d}] {prefix} 存在性（{tag}）{label} → {qid}（尝试 {tries} 次）", "success")

    # ---- 选择性：2_ 随机票（题型由行为约束推导） ----
    sel_fail = [0] * len(selective_rows)
    for idx, row in enumerate(selective_rows):
        crit, beh = row.get("criterion", "comprehensive"), row.get("behavior", "none")
        constraints_ = [] if beh == "none" else [beh]
        for _ in range(int(row.get("count") or 0)):
            qid, tries, err, sq, _rand, _hint = generate_one(None, 0, [], crit, constraints_,
                                                             "2_", "random_tickets", random_tickets_density)
            details.append({"question_id": qid or f"2_B{timestamp}_{sq:04d}",
                            "row": f"{criterion_cn(crit)}/{behavior_cn(beh)}",
                            "attempts": tries, "ok": qid is not None, "error": err})
            if qid is None:
                sel_fail[idx] += 1
                log(f"[{sq:04d}] 2_ 选择性 {criterion_cn(crit)}({behavior_cn(beh)}) 失败：{err}", "error")
            else:
                log(f"[{sq:04d}] 2_ 选择性 {criterion_cn(crit)}({behavior_cn(beh)}) → {qid}（尝试 {tries} 次）",
                    "success")

    ok = sum(1 for d in details if d["ok"])
    log(f"出题完成：成功 {ok} / 失败 {len(details) - ok}（共 {len(details)} 题）",
        "success" if ok == len(details) else "warn")
    return {
        "total": len(details),
        "success": ok,
        "failed": len(details) - ok,
        "details": details,
        "exists": [{"category": r.get("category", ""), "name": r.get("name", ""),
                    "has_interference_failed": exists_fail[i]["has_interference"],
                    "no_interference_failed": exists_fail[i]["no_interference"]}
                   for i, r in enumerate(distribution)],
        "selective": [{"criterion": r.get("criterion", ""), "behavior": r.get("behavior", ""),
                       "failed": sel_fail[i]} for i, r in enumerate(selective_rows)],
    }


# ============================================================
# CLI
# ============================================================

def _parse_seats(s: str) -> Dict[str, float]:
    """'20,30,50' → {"class0":20, "class1":30, "class2":50}（和须=100）"""
    parts = [float(x) for x in s.split(",")]
    if len(parts) != 3 or abs(sum(parts) - 100) > 0.001 or min(parts) < 0:
        raise argparse.ArgumentTypeError(f"座位权重须为 3 个非负数且和=100，当前 {s}")
    return {"class0": parts[0], "class1": parts[1], "class2": parts[2]}


def main():
    ap = argparse.ArgumentParser(description="step2 批量出题（1.xlsx 分布表 + 2.xlsx 站对表）")
    ap.add_argument("--railway-db", required=True, help="基础数据库路径（只读）")
    ap.add_argument("--prices-db", required=True, help="票价数据库路径（只读）")
    ap.add_argument("--question-dir", required=True, help="题目输出目录（生成 {qid}.db + metadata.json）")
    ap.add_argument("--metadata", default=None, help="元数据文件路径（默认 {question-dir}/metadata.json）")
    ap.add_argument("--distribution", required=True, help="1.xlsx 题目分布表路径")
    ap.add_argument("--stations", required=True, help="2.xlsx 到发站对表路径")
    ap.add_argument("--seat-weights", type=_parse_seats, default="20,30,50",
                    help="特等座,一等座,二等座 抽样权重 %%（和=100），默认 20,30,50")
    ap.add_argument("--interference-density", type=float, default=0.02,
                    help="干扰密度（1_，票数严格<人数），0=关闭，默认 0.02")
    ap.add_argument("--random-tickets-density", type=float, default=0.02,
                    help="随机票密度（2_，0.5~1.5×人数），0=关闭，默认 0.02")
    ap.add_argument("--max-retries", type=int, default=40, help="每题重试上限，默认 40")
    ap.add_argument("--receipt", default=None, help="失败回执 xlsx 输出路径（可选）")
    args = ap.parse_args()

    db.configure_paths(railway_db=args.railway_db, prices_db=args.prices_db,
                       question_dir=args.question_dir, metadata=args.metadata)
    if not os.path.isfile(args.distribution):
        sys.exit(f"❌ 分布表不存在: {args.distribution}")
    if not os.path.isfile(args.stations):
        sys.exit(f"❌ 站对表不存在: {args.stations}")

    distribution = parse_distribution_xlsx(open(args.distribution, "rb").read())
    stations = parse_stations_xlsx(open(args.stations, "rb").read())
    if not stations:
        sys.exit("❌ 站对表解析为空")
    n_exists = sum((r["has_interference"] + r["no_interference"]) for r in distribution["exists"])
    n_sel = sum(r["count"] for r in distribution["selective"])
    print(f"分布表解析：总题目={distribution['total']}，存在性 {n_exists} + 选择性 {n_sel}；"
          f"站对 {len(stations)} 对")

    report = run_batch(distribution["exists"], distribution["selective"], stations,
                       args.seat_weights, args.interference_density,
                       args.random_tickets_density, args.max_retries)

    if args.receipt:
        data = generate_failure_report_xlsx(report)
        with open(args.receipt, "wb") as f:
            f.write(data)
        print(f"失败回执已写入: {args.receipt}")

    print("\n===== 批量出题回执 =====")
    print(f"总题目 {report['total']}｜成功 {report['success']}｜失败 {report['failed']}")
    for e in report["exists"]:
        print(f"  {e['category']}/{e['name']}: 干扰失败 {e['has_interference_failed']}，"
              f"无干扰失败 {e['no_interference_failed']}")
    for s in report["selective"]:
        print(f"  选择性 {criterion_cn(s['criterion'])}/{behavior_cn(s['behavior'])}: 失败 {s['failed']}")


if __name__ == "__main__":
    main()
