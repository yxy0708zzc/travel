"""
assist/filter_pairs_by_direct.py —— 按直达车次数分区段采样站对表

流程：**先举、再抽、后写**。
① 先举：一次遍历全库经停，穷举统计每个有序站对（同车 A 先于 B）的直达车
   次数（约 10 万条，秒级；口径与出题端 get_routes_between 一致，不去重
   同车多号）；
② 再抽：各区间（lo≥1）从统计中精确筛选 k∈[lo,hi] 的候选（排除已收对）
   随机抽取 count 个——无命中率问题，高直达区间也能收满；候选不足则全收
   并警告；
③ min=0 区间（无直达）：候选不在穷举统计里，走随机站对法（随机对绝大多数
   为 0 直达，近乎必中），预算 = 个数 × --attempts-multiplier 仅用于防死循环；
④ 后写：按区间分块输出，每块上方一行区段元数据（前 2 列空，第 4 列=范围、
   第 5 列=实际收集个数）；parse_stations_xlsx 跳过元数据行与空行（连续 ≥4
   空行=表结束哨兵），step2 可直接消费。

跨区间全局去重：同一有序站对只归一个区间。

用法（分隔符用逗号——cmd 下 | 是管道符会拆参数；如需 | 请给整个参数加引号）：
    python assist/filter_pairs_by_direct.py --railway-db data/railway.db ^
        --ranges 0~10,20~60,100~200 --counts 10,20,30 ^
        --attempts-multiplier 300 --out 2_站对表.xlsx [--seed 42]
"""

"""
assist/filter_pairs_by_direct.py —— 按直达车次数分区段采样站对表

流程：**先举、再抽、后写**。
① 先举：一次遍历全库经停，穷举统计每个有序站对（同车 A 先于 B）的直达车
   次数（约 10 万条，秒级；口径与出题端 get_routes_between 一致，不去重
   同车多号）；
② 再抽：各区间（lo≥1）从统计中精确筛选 k∈[lo,hi] 的候选（排除已收对）
   随机抽取 count 个——无命中率问题，高直达区间也能收满；候选不足则全收
   并警告；
③ min=0 区间（无直达）：候选不在穷举统计里，走随机站对法（随机对绝大多数
   为 0 直达，近乎必中），预算 = 个数 × --attempts-multiplier 仅用于防死循环；
④ 后写：按区间分块输出，每块上方一行区段元数据（前 2 列空，第 4 列=范围、
   第 5 列=实际收集个数）；parse_stations_xlsx 跳过元数据行与空行（连续 ≥4
   空行=表结束哨兵），step2 可直接消费。

跨区间全局去重：同一有序站对只归一个区间。

用法（分隔符用逗号——cmd 下 | 是管道符会拆参数；如需 | 请给整个参数加引号）：
    python assist/filter_pairs_by_direct.py --railway-db data/railway.db ^
        --ranges 0~10,20~60,100~200 --counts 10,20,30 ^
        --attempts-multiplier 300 --out 2_站对表.xlsx [--seed 42]
"""

import argparse
import os
import random
import re
import sqlite3
import sys
from itertools import groupby
from typing import Dict, List, Tuple


def _parse_ranges(s: str):
    """区间列表 → [(lo, hi), ...]（闭区间，允许 min==max）。
    分隔符支持 逗号 / | / 空白（cmd 下 | 是管道符，不加引号会被当命令拆开——推荐逗号）。"""
    out = []
    for part in re.split(r"[|,\s]+", s.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*~\s*(\d+)", part)
        if not m:
            raise argparse.ArgumentTypeError(
                f"区间格式错误: {part!r}（应为 下限~上限，如 0~10）")
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            raise argparse.ArgumentTypeError(f"区间下限大于上限: {part!r}")
        out.append((lo, hi))
    if not out:
        raise argparse.ArgumentTypeError("区间列表为空")
    return out


def _parse_counts(s: str):
    """个数列表（分隔符同区间：逗号 / | / 空白）。"""
    out = []
    for part in re.split(r"[|,\s]+", s.strip()):
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(f"个数格式错误: {part!r}")
        if v < 1:
            raise argparse.ArgumentTypeError(f"个数须 ≥1: {part!r}")
        out.append(v)
    if not out:
        raise argparse.ArgumentTypeError("个数列表为空")
    return out


def parse_args():
    ap = argparse.ArgumentParser(
        description="按直达车次数分区段采样站对表（先全库穷举统计，各区间精确筛选抽取）")
    ap.add_argument("--railway-db", required=True, help="基础数据库路径（只读）")
    ap.add_argument("--ranges", type=_parse_ranges, required=True,
                    help="直达数区间列表：逗号分隔（cmd 下勿用 |，是管道符），每段 下限~上限，"
                         "如 0~10,20~60,100~200（允许 min=0 与 min==max 精确匹配）")
    ap.add_argument("--counts", type=_parse_counts, required=True,
                    help="每区间输出个数：逗号分隔，个数须与 --ranges 一致，如 10,20,30")
    ap.add_argument("--attempts-multiplier", type=int, required=True,
                    help="仅 min=0 区间使用：尝试上限 = 该区间个数 × 此倍数（≥1；min≥1 区间走穷举筛选，无命中率问题）")
    ap.add_argument("--out", required=True, help="输出 xlsx 路径（2.xlsx 兼容，含区段元数据行）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    return ap.parse_args()


def _enumerate_direct_counts(conn: sqlite3.Connection) -> Dict[Tuple[str, str], int]:
    """先举：一次遍历全库经停，累计每个有序站对（同车 A 先于 B）的直达车次数。
    只覆盖 k≥1 的站对；口径与出题端 get_routes_between 一致（不去重同车多号）。"""
    direct_counts: Dict[Tuple[str, str], int] = {}
    rows = conn.execute(
        "SELECT train_num, station_id FROM train_stops ORDER BY train_num, stop_no").fetchall()
    for _tn, group in groupby(rows, key=lambda r: r[0]):
        ids = [sid for _t, sid in group]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                key = (ids[i], ids[j])
                direct_counts[key] = direct_counts.get(key, 0) + 1
    return direct_counts


def _pick_enumerated(rng: random.Random, lo: int, hi: int, need: int,
                     direct_counts: Dict[Tuple[str, str], int], collected_set: set):
    """lo≥1 区间：从穷举统计中精确筛选候选（排除已收对），随机抽取 need 个。
    返回 (pairs, pool_size, short)。"""
    cands = [(pair, k) for pair, k in direct_counts.items()
             if lo <= k <= hi and pair not in collected_set]
    chosen = cands if len(cands) <= need else rng.sample(cands, need)
    pairs = [(a, b, k) for (a, b), k in chosen]
    for a, b, _k in pairs:
        collected_set.add((a, b))
    return pairs, len(cands), len(pairs) < need


def _sample_zero_direct(rng: random.Random, hi: int, need: int, max_attempts: int,
                        station_ids: List[str],
                        direct_counts: Dict[Tuple[str, str], int], collected_set: set):
    """min=0 区间：随机站对法——候选不在穷举统计中即直达 0；随机对绝大多数
    为 0 直达，近乎必中。返回 (pairs, attempts, dup, hit_limit)。"""
    pairs = []
    dup = 0
    hit_limit = True
    attempts = 0
    for attempts in range(1, max_attempts + 1):
        a, b = rng.sample(station_ids, 2)
        if (a, b) in collected_set:
            dup += 1
            continue
        if direct_counts.get((a, b), 0) <= hi:   # 无直达（不在统计中）或 ≤hi 都命中
            pairs.append((a, b, 0))
            collected_set.add((a, b))
            if len(pairs) >= need:
                hit_limit = False
                break
    return pairs, attempts, dup, hit_limit


def main():
    args = parse_args()
    ranges, counts = args.ranges, args.counts
    if len(ranges) != len(counts):
        sys.exit(f"❌ 参数错误：区间个数({len(ranges)})与个数列表({len(counts)})必须一致")
    if args.attempts_multiplier < 1:
        sys.exit(f"❌ 参数错误：--attempts-multiplier 须 ≥1，当前 {args.attempts_multiplier}")
    if not os.path.isfile(args.railway_db):
        sys.exit(f"❌ 基础数据库不存在: {args.railway_db}")

    rng = random.Random(args.seed)
    print(f"分区段采样（先穷举统计）：{len(ranges)} 个区间"
          + (f"，seed={args.seed}" if args.seed is not None else ""))

    conn = sqlite3.connect(args.railway_db)
    try:
        stations = conn.execute("SELECT station_id, station_name FROM stations").fetchall()
        if len(stations) < 2:
            sys.exit("❌ stations 表不足 2 个车站，无法组成站对")
        station_ids = [sid for sid, _ in stations]
        id_to_name = dict(stations)

        direct_counts = _enumerate_direct_counts(conn)
        print(f"穷举统计完成：{len(direct_counts)} 个有直达的有序站对")

        collected_set = set()   # 跨区间全局去重：同一有序对只归一个区间
        segments = []           # [(lo, hi, need, pairs, short)]
        for (lo, hi), need in zip(ranges, counts):
            if lo >= 1:
                pairs, pool, short = _pick_enumerated(
                    rng, lo, hi, need, direct_counts, collected_set)
                print(f"[区间 {lo}~{hi}] 候选池 {pool} 对，抽取 {len(pairs)}"
                      + ("　⚠️ 候选池不足，未收满" if short else ""))
                segments.append((lo, hi, need, pairs, short))
            else:
                max_attempts = need * args.attempts_multiplier
                print(f"[区间 {lo}~{hi}] 目标 {need} 对（无直达，随机站对法近乎必中），"
                      f"尝试上限 {max_attempts}")
                pairs, attempts, dup, short = _sample_zero_direct(
                    rng, hi, need, max_attempts, station_ids, direct_counts, collected_set)
                if attempts:
                    print(f"  尝试 {attempts}：命中 {len(pairs)}，重复拒绝 {dup}"
                          f"（命中率 {len(pairs) / attempts:.1%}）"
                          + ("　⚠️ 上限打满，未收满" if short else ""))
                else:
                    print("  尝试 0 次")
                segments.append((lo, hi, need, pairs, short))
    finally:
        conn.close()

    # ---- 控制台明细 ----
    seq = 0
    for lo, hi, _need, pairs, *_ in segments:
        print(f"— 区间 {lo}~{hi}")
        for a, b, direct in pairs:
            seq += 1
            print(f"  {seq:>3}. {id_to_name[a]} → {id_to_name[b]}（直达 {direct} 趟）")

    # ---- 写 xlsx：表头（含第 4/5 列）+ 每区间【元数据行在上 + 数据行】----
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "到发站对"
    ws.cell(row=1, column=1, value="出发站")
    ws.cell(row=1, column=2, value="到达站")
    ws.cell(row=1, column=4, value="范围")
    ws.cell(row=1, column=5, value="个数")
    r = 2
    for lo, hi, need, pairs, *_ in segments:
        # 区段元数据行（在该类上方）：前 2 列空，第 4 列=范围、第 5 列=实际收集数
        ws.cell(row=r, column=4, value=f"{lo}~{hi}")
        ws.cell(row=r, column=5, value=len(pairs))
        r += 1
        for a, b, _direct in pairs:
            ws.cell(row=r, column=1, value=id_to_name[a])
            ws.cell(row=r, column=2, value=id_to_name[b])
            r += 1
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    wb.save(args.out)
    total = sum(len(s[3]) for s in segments)
    print(f"已写出 {total} 对（{len(segments)} 个区间）→ {args.out}")


if __name__ == "__main__":
    main()
