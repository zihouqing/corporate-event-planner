#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业活动预算骨架计算器

把总额、人数、天数、类型、城市等级，算成一张科目分配表 + 承载力校验。
输出的所有金额都是「骨架值」，必须用真实报价回填重算后才算方案。

用法:
    python3 budget_calc.py --total 80000 --people 30 --days 2 --type teambuilding --city-tier 1
    python3 budget_calc.py --total 200000 --people 150 --days 1 --type annual --json
    python3 budget_calc.py --per-head 1200 --people 30 --days 2 --type teambuilding
"""

import argparse
import json
import math
import sys
import unicodedata

# ---------------------------------------------------------------- 数据表
# 档位均为「新一线城市」口径，判定前先把人均按城市系数归一化
TIERS = {
    "teambuilding": {
        1: [(150, 280, "经济"), (280, 500, "标准"), (500, 900, "品质"), (900, 10**9, "高端")],
        "overnight": [(300, 500, "经济"), (500, 900, "标准"), (900, 1600, "品质"), (1600, 10**9, "高端")],
    },
    "annual": {1: [(300, 600, "简约"), (600, 1200, "标准"), (1200, 2500, "品质"), (2500, 10**9, "高端")]},
    "client": {1: [(600, 2000, "标准"), (2000, 10**9, "高端")]},
    "training": {1: [(500, 1500, "标准"), (1500, 10**9, "高端")]},
}

CITY_COEF = {1: 1.22, 2: 1.00, 3: 0.85, 4: 0.75}
CITY_LABEL = {1: "一线", 2: "新一线", 3: "二线", 4: "三线及以下"}

# 科目配比（顺序即输出顺序），合计必须为 1.0
RATIOS = {
    ("teambuilding", True): [
        ("住宿", "酒店/民宿房费", 0.340),
        ("餐饮", "正餐+早餐", 0.250),
        ("交通", "往返+当地用车", 0.150),
        ("场地/活动", "场地费+拓展项目", 0.100),
        ("物料/礼品", "队服/道具/伴手礼", 0.065),
        ("执行", "教练/主持人/摄影", 0.080),
        ("保险", "意外险", 0.015),
    ],
    ("teambuilding", False): [
        ("餐饮", "正餐", 0.315),
        ("交通", "包车", 0.280),
        ("场地/活动", "场地+项目", 0.215),
        ("执行", "教练/主持人", 0.100),
        ("物料/礼品", "道具/伴手礼", 0.080),
        ("保险", "意外险", 0.010),
    ],
    ("annual", None): [
        ("场租/餐饮", "宴会厅+桌餐+服务费", 0.450),
        ("舞台/灯光", "LED/音响/桁架", 0.150),
        ("住宿", "客房", 0.150),
        ("奖品/礼品", "抽奖+伴手礼", 0.100),
        ("演出/主持", "节目+司仪", 0.070),
        ("物料/布置", "签到/氛围/指引", 0.050),
        ("摄影摄像", "录制+快剪", 0.030),
    ],
    ("client", None): [
        ("场地/餐饮", "宴会厅+餐", 0.500),
        ("伴手礼", "客户礼品", 0.200),
        ("布置", "氛围/花艺", 0.100),
        ("演出/主持", "节目+司仪", 0.080),
        ("执行", "策划执行", 0.070),
        ("摄影摄像", "记录", 0.050),
    ],
    ("training", None): [
        ("场地/住宿", "会议厅+客房", 0.400),
        ("餐饮", "正餐+茶歇", 0.220),
        ("讲师/课程", "课酬+版权", 0.200),
        ("交通", "往返", 0.075),
        ("物料/教材", "手册/文具", 0.065),
        ("其他", "杂项", 0.040),
    ],
}

TYPE_LABEL = {"teambuilding": "团建", "annual": "年会", "client": "客户答谢", "training": "培训"}


# ---------------------------------------------------------------- 工具
def reserve_rate(args) -> float:
    """机动金率：用户指定优先，其次按规模与类型推断"""
    if args.reserve is not None:
        return args.reserve
    if args.people >= 100:
        return 0.05
    if args.type in ("client",):
        return 0.08
    return 0.08


def pick_ratio(args):
    key = (args.type, args.days >= 2) if args.type == "teambuilding" else (args.type, None)
    return RATIOS[key]


def tier_table(args):
    if args.type == "teambuilding":
        return TIERS["teambuilding"]["overnight" if args.days >= 2 else 1]
    return TIERS[args.type][1]


def judge_tier(per_head_norm: float, args):
    table = tier_table(args)
    for lo, hi, name in table:
        if lo <= per_head_norm < hi:
            return name, lo, hi
    if per_head_norm < table[0][0]:
        return "低于经济档", table[0][0], table[0][1]
    lo, hi, name = table[-1]
    return name, lo, hi


def fmt_num(v) -> str:
    """整数值不显示小数点：2.0 → 2，0.5 → 0.5"""
    fv = float(v)
    return str(int(fv)) if fv.is_integer() else f"{fv:g}"


def dwidth(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int, align: str = "left") -> str:
    s = str(s)
    gap = width - dwidth(s)
    if gap <= 0:
        return s
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def money(v) -> str:
    return f"¥{int(round(v)):,}"


# ---------------------------------------------------------------- 主计算
def compute(args):
    rate = reserve_rate(args)
    people = args.people
    days = args.days

    if args.per_head is not None:
        # 反向：给人均上限反推总额
        total = args.per_head * people * days / (1 - rate)
    else:
        total = float(args.total)

    reserve = total * rate
    usable = total - reserve
    per_head_day = usable / people / days
    coef = CITY_COEF[args.city_tier]
    per_head_norm = per_head_day / coef
    tier_name, lo, hi = judge_tier(per_head_norm, args)

    ratios = pick_ratio(args)
    line_items = []
    for name, detail, r in ratios:
        amt = usable * r
        line_items.append(
            {"subject": name, "detail": detail, "ratio": r, "amount": amt,
             "per_head": amt / people}
        )

    # 承载力
    tables = math.ceil(people / 10)
    hall_area_min = tables * 12
    hall_area_rec = tables * 15
    rooms = math.ceil(people / 2)
    buses = math.ceil(people / 45)

    # 最低可行总额：按该类型经济档下限推算（与当前档位无关）
    econ_floor = tier_table(args)[0][0]
    min_total = econ_floor * coef * people * days / (1 - rate)

    return {
        "input": {
            "total": round(total, 2),
            "people": people,
            "days": days,
            "type": args.type,
            "city_tier": args.city_tier,
            "reserve_rate": rate,
        },
        "summary": {
            "reserve": round(reserve, 2),
            "usable": round(usable, 2),
            "per_head_day": round(per_head_day, 2),
            "per_head_day_normalized": round(per_head_norm, 2),
            "city_coefficient": coef,
            "tier": tier_name,
            "tier_range": [lo, hi if hi < 10**8 else None],
            "min_viable_total": round(min_total, 2),
        },
        "line_items": [
            {**item, "amount": round(item["amount"], 2), "per_head": round(item["per_head"], 2)}
            for item in line_items
        ],
        "capacity": {
            "round_tables": tables,
            "hall_area_min_sqm": hall_area_min,
            "hall_area_recommended_sqm": hall_area_rec,
            "rooms_needed": rooms,
            "buses_49seat": buses,
        },
    }


def render(res: dict, args) -> str:
    i, s, cap = res["input"], res["summary"], res["capacity"]
    out = []
    bar = "=" * 74
    thin = "-" * 74

    title = f"预算骨架 · {TYPE_LABEL[i['type']]} · {fmt_num(i['days'])} 天 · {i['people']} 人 · {CITY_LABEL[i['city_tier']]}"
    out.append(bar)
    out.append(title)
    out.append(bar)
    out.append(f"{pad('总预算', 18)}{pad(money(i['total']), 14, 'right')}")
    reserve_pct = i["reserve_rate"] * 100
    out.append(f"{pad(f'机动金（{reserve_pct:.0f}%）', 18)}{pad(money(s['reserve']), 14, 'right')}")
    out.append(f"{pad('可支配', 18)}{pad(money(s['usable']), 14, 'right')}")
    out.append(f"{pad('人均日成本', 18)}{pad(money(s['per_head_day']), 14, 'right')}   → {s['tier']}")
    out.append("")
    out.append(thin)
    out.append(f"{pad('科目', 14)}{pad('明细', 24)}{pad('金额', 14, 'right')}{pad('占比', 9, 'right')}{pad('人均', 11, 'right')}")
    out.append(thin)
    for item in res["line_items"]:
        pct = f"{item['ratio'] * 100:.1f}%"
        out.append(
            f"{pad(item['subject'], 14)}{pad(item['detail'], 24)}"
            f"{pad(money(item['amount']), 14, 'right')}"
            f"{pad(pct, 9, 'right')}"
            f"{pad(money(item['per_head']), 11, 'right')}"
        )
    out.append(thin)
    out.append(f"{pad('合计（可支配）', 38)}{pad(money(s['usable']), 14, 'right')}{pad('100.0%', 9, 'right')}")
    out.append(f"{pad('机动金', 38)}{pad(money(s['reserve']), 14, 'right')}")
    out.append(f"{pad('总计', 38)}{pad(money(i['total']), 14, 'right')}")
    out.append("")

    out.append(bar)
    out.append("承载力校验（按人数推算的最小配置）")
    out.append(bar)
    out.append(f"  圆桌            {cap['round_tables']} 桌（10 人/桌）")
    out.append(f"  宴会厅面积      ≥ {cap['hall_area_min_sqm']} ㎡，建议 {cap['hall_area_recommended_sqm']} ㎡（含主通道与舞台前区）")
    out.append(f"  客房            {cap['rooms_needed']} 间（标间 2 人/间，单住需另加）")
    out.append(f"  大巴            {cap['buses_49seat']} 辆（49 座按实载 45 人）")
    if args.days >= 2:
        out.append(f"  总客房夜数      {fmt_num(cap['rooms_needed'] * (args.days - 1))} 间夜")
    out.append("")

    # 档位提示
    if s["tier"] == "低于经济档":
        out.append(f"[!] 人均 ¥{s['per_head_day']:.0f}/天（新一线口径 ¥{s['per_head_day_normalized']:.0f}）"
                   f"低于经济档下限 ¥{s['tier_range'][0]}。")
        out.append(f"    最低可行总额约 {money(s['min_viable_total'])}，当前缺 {money(s['min_viable_total'] - i['total'])}。")
        out.append("    建议：减天数 / 减人数 / 降目的地档次，三选一，不要硬做。")
    elif s["tier"].startswith("高端"):
        out.append(f"[i] 人均 ¥{s['per_head_day_normalized']:.0f}（归一化）已达高端档（¥{s['tier_range'][0]}+）。")
        out.append("    预算充裕，建议把余量放在体验项（特色场地、定制活动、好餐），而不是加价订房。")
    else:
        out.append(f"[i] 档位判定：{s['tier']}（新一线口径 ¥{s['tier_range'][0]}-{s['tier_range'][1]}，已按系数 {s['city_coefficient']} 换算）。")
    out.append("")
    out.append("* 以上为骨架值，不是报价。找到真实场地后必须回填价格重算，偏差 >5% 要调科目。")
    out.append("* 报价不含往返大交通票款时，需在交通科目外单列。")
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="企业活动预算骨架计算器")
    p.add_argument("--total", type=float, help="预算总额（元）")
    p.add_argument("--per-head", type=float, dest="per_head", help="人均上限（元/人/天），反向推总额")
    p.add_argument("--people", type=int, required=True, help="人数")
    p.add_argument("--days", type=float, required=True, help="天数（0.5 表示半天）")
    p.add_argument("--type", required=True, choices=list(TYPE_LABEL), help="活动类型")
    p.add_argument("--city-tier", type=int, default=2, choices=[1, 2, 3, 4], help="城市等级 1-4")
    p.add_argument("--reserve", type=float, help="机动金率，如 0.08")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args()

    if args.total is None and args.per_head is None:
        p.error("必须提供 --total 或 --per-head 之一")
    if args.people <= 0 or args.days <= 0:
        p.error("--people 与 --days 必须大于 0")

    res = compute(args)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(res, args))


if __name__ == "__main__":
    main()
