#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高德 POI 核验工具 —— 确认酒店/场地是否真实存在

只做三件事：查点位、算匹配度、算两点驾车距离。
Key 优先级：--key 参数 > 环境变量 AMAP_KEY > ~/.amap_key > 随包的 amap_key.txt。
必须是「Web 服务」类型的 Key，不是「Web 端 JS API」。

用法:
    python3 poi_check.py --name "深圳湾万丽酒店" --city 深圳
    python3 poi_check.py --batch venues.txt          # 每行：名称|城市
    python3 poi_check.py --distance "113.93,22.52" "114.06,22.54"

退出码:
    0  核验通过（至少一个点位为 ✅ 或 ⚠️）
    1  参数/网络错误
    2  无 AMAP_KEY，需降级到联网搜索核验
    3  全部点位查无此店
"""

import argparse
import difflib
import json
import os
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

PLACE_API = "https://restapi.amap.com/v3/place/text"
DRIVE_API = "https://restapi.amap.com/v3/direction/driving"

INFOCODE_HINT = {
    "10001": "Key 不正确或已过期",
    "10002": "该 Key 没有调用此服务的权限",
    "10003": "已超出当日访问量",
    "10004": "单位时间内访问过于频繁（QPS 超限）",
    "10009": "Key 与绑定平台不符 —— 你需要的是「Web 服务」类型的 Key，不是「Web 端 JS API」",
    "10021": "QPS 超出限制",
    "20000": "请求参数非法",
    "20800": "规划点（起终点）超出服务范围",
    "20802": "规划点附近没有道路",
}

# 频率类错误码，值得退避重试（日配额超限 10003 重试无意义，不列）
QPS_CODES = ("10021", "10004")

# 子点位里出现这些词的，可作为 L4 承载力核验的线索（能直接问到厅的桌数）
HALL_KEYWORDS = ("宴会厅", "会议厅", "多功能厅", "大宴会", "会议室", "会展")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_ROOT = os.path.dirname(_HERE)

# Key 查找顺序：--key 参数 → 环境变量 → 本机密钥文件 → 随包密钥（免配置）
KEY_FILES = [
    os.path.expanduser("~/.amap_key"),
    os.path.expanduser("~/.workbuddy/amap_key"),
    os.path.join(_HERE, "amap_key.txt"),
    os.path.join(_SKILL_ROOT, "amap_key.txt"),
]


def resolve_key(cli_key: str | None) -> tuple:
    """返回 (key, 来源说明)。优先级：命令行 > 环境变量 > 密钥文件。"""
    if cli_key:
        return cli_key.strip(), "--key 参数"
    env = os.environ.get("AMAP_KEY", "").strip()
    if env:
        return env, "环境变量 AMAP_KEY"
    for path in KEY_FILES:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
            if val:
                return val, path
        except OSError:
            continue
    return "", ""


def mask(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


# ---------------------------------------------------------------- 工具
def dwidth(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, width: int, align: str = "left") -> str:
    s = str(s)
    gap = width - dwidth(s)
    if gap <= 0:
        return s
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def http_json(url: str, params: dict, timeout: int = 15, insecure: bool = False) -> dict:
    qs = urllib.parse.urlencode(params, safe=",|")
    full = f"{url}?{qs}"
    req = urllib.request.Request(full, headers={"User-Agent": "event-planner/1.0"})
    ctx = None
    if insecure:
        import ssl
        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def normalize(name: str) -> str:
    """去掉空格和括号内的分店后缀，用于相似度比较"""
    s = name.strip().replace(" ", "").replace("\u3000", "")
    for left, right in (("（", "）"), ("(", ")")):
        while left in s and right in s:
            i, j = s.find(left), s.find(right)
            if i < j:
                s = s[:i] + s[j + 1:]
            else:
                break
    return s


# 子设施后缀：命中这些说明只找到「酒店的洗手间」，不是酒店本体
FACILITY_SUFFIXES = (
    "洗手间", "厕所", "卫生间", "停车场", "停车点", "停车库", "大堂", "入口", "出口",
    "电梯", "充电桩", "售票处", "存包处", "atm", "取款机", "保安亭", "垃圾",
)


def is_facility_of(candidate: str, target: str) -> bool:
    """候选名 = 目标名（或高度相似的变体）+ 纯设施后缀，说明主体没找到，只找到了附属设施。

    不能只判断 startswith：归一化后「观澜湖度假酒店(深圳店)-大堂」的开头是「观澜湖」，
    而输入是「深圳观澜湖度假酒店」，前缀对不上但仍然是同一家的子设施。
    所以改成剥掉设施后缀再比相似度。
    """
    c, t = normalize(candidate).lower(), normalize(target).lower()
    if not c or not t or len(c) <= 2:
        return False
    for suf in FACILITY_SUFFIXES:
        for tail in ("-" + suf, "·" + suf, suf):
            if not c.endswith(tail):
                continue
            stem = c[: -len(tail)].strip("-· ")
            if not stem:
                continue
            if stem == t or difflib.SequenceMatcher(None, stem, t).ratio() >= 0.6:
                return True
    return False


def similarity(target: str, candidate: str) -> float:
    a, b = normalize(target), normalize(candidate)
    if not a or not b:
        return 0.0
    base = difflib.SequenceMatcher(None, a, b).ratio()
    if is_facility_of(candidate, target):
        # 只命中子设施（洗手间/停车场/大堂），主体未找到，必须压到「查无此店」
        return min(base, 0.50)
    if a in b:
        # 候选名是更完整的规范全称（输入「万丽酒店」→ 命中「深圳湾万丽酒店」），可信
        base = max(base, 0.88)
    elif b in a:
        # 候选名比输入更泛化（输入「阿那亚社区礼堂」→ 只命中「阿那亚」园区），按长度比打折，
        # 否则会把园区名/城市名误判成已核验的场地
        base = min(base, 0.85 * len(b) / len(a))
    return base


PLACEHOLDER_TOKENS = ("xx", "某某", "待定", "tbd", "abc酒店", "某酒店")


def has_placeholder(name: str) -> bool:
    low = normalize(name).lower()
    return any(tok in low for tok in PLACEHOLDER_TOKENS)


def is_child_poi(child: str, parent: str) -> bool:
    """判断候选是否真是该主体的子点位。

    不加这层会把「大梅沙京基海湾大酒店-会议厅」算成「大梅沙京基洲际度假酒店」的宴会厅，
    两家其实是隔壁邻居。用最长公共子串占比兜住。
    """
    c, p = normalize(child), normalize(parent)
    if not c or not p:
        return False
    m = difflib.SequenceMatcher(None, c, p).find_longest_match(0, len(c), 0, len(p))
    return m.size >= max(4, len(p) * 0.6)


def verdict(score: float) -> tuple:
    if score >= 0.85:
        return "✅ 已核验", "high"
    if score >= 0.60:
        return "⚠️ 待确认", "suspect"
    return "❌ 查无此店", "miss"


def parse_num(v) -> float | None:
    """高德空值返回 []，有值时可能是字符串或数字，统一成 float 或 None"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def api_error(data: dict) -> str | None:
    if str(data.get("status")) == "1":
        return None
    code = str(data.get("infocode", ""))
    msg = data.get("info", "未知错误")
    hint = INFOCODE_HINT.get(code)
    return f"[{code}] {msg}" + (f" —— {hint}" if hint else "")


# ---------------------------------------------------------------- 核验单点
def check_one(name: str, city: str, key: str, insecure: bool, retries: int = 3) -> dict:
    if has_placeholder(name):
        return {
            "query": name, "city": city, "status": "❌ 查无此店",
            "error": "名称含占位符（XX / 某某 / 待定），不是真实点位。请先确定具体酒店名再说。",
            "candidates": [],
        }
    params = {
        "key": key,
        "keywords": name,
        "city": city,
        "citylimit": "true",
        "offset": "10",
        "page": "1",
        "extensions": "all",
    }
    # 个人 Key 的 QPS 很低，连续请求必然撞 10021，这里做退避重试
    data, last_exc = None, None
    for attempt in range(retries):
        try:
            data = http_json(PLACE_API, params, insecure=insecure)
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
            continue
        if str(data.get("infocode", "")) in QPS_CODES and attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
            continue
        break

    if data is None:
        return {"query": name, "city": city, "status": "❌ 网络错误",
                "error": f"{last_exc}（已重试 {retries} 次）", "candidates": []}

    err = api_error(data)
    if err:
        return {"query": name, "city": city, "status": "❌ 接口错误", "error": err, "candidates": []}

    pois = data.get("pois") or []
    if isinstance(pois, str):
        pois = []

    cands = []
    for p in pois[:10]:
        cname = p.get("name", "")
        score = similarity(name, cname)
        label, level = verdict(score)
        biz = p.get("biz_ext") or {}
        cands.append({
            "name": cname,
            "address": p.get("address") if isinstance(p.get("address"), str) else "",
            "district": p.get("adname", ""),
            "city": p.get("cityname", ""),
            "tel": p.get("tel") if isinstance(p.get("tel"), str) else "",
            "location": p.get("location", ""),
            "poi_id": p.get("id", ""),
            "typecode": p.get("typecode", ""),
            "rating": parse_num(biz.get("rating")),
            "cost": parse_num(biz.get("cost")),
            "score": round(score, 3),
            "verdict": label,
            "level": level,
        })
    cands.sort(key=lambda c: c["score"], reverse=True)

    if not cands:
        return {
            "query": name, "city": city, "status": "❌ 查无此店",
            "error": f"「{city}」范围内无匹配结果。换个关键词重试：品牌+地名，如「万丽 深圳湾」",
            "candidates": [],
        }

    top = cands[0]
    halls = [
        c for c in cands
        if c["name"] != top["name"]
        and any(k in c["name"] for k in HALL_KEYWORDS)
        and is_child_poi(c["name"], top["name"])
    ]
    result = {
        "query": name, "city": city,
        "status": top["verdict"],
        "matched_name": top["name"],
        "address": top["address"],
        "district": top["district"],
        "tel": top["tel"],
        "location": top["location"],
        "poi_id": top["poi_id"],
        "score": top["score"],
        "rating": top["rating"],
        "cost": top["cost"],
        "candidates": cands,
        "halls": halls[:5],
    }
    if is_facility_of(top["name"], name):
        result["error"] = (f"只命中该场地的子设施「{top['name']}」，主体未找到。"
                           f"请换关键词重试：去掉城市前缀，或用「品牌 + 区域」。")
    elif top["level"] == "miss":
        result["error"] = "最高相似度低于 0.60，视为查无此店。请核对名称或换关键词重试一次。"
    return result


# ---------------------------------------------------------------- 距离
def check_distance(origin: str, destination: str, key: str, insecure: bool) -> dict:
    params = {"key": key, "origin": origin, "destination": destination, "extensions": "base"}
    data = http_json(DRIVE_API, params, insecure=insecure)
    err = api_error(data)
    if err:
        return {"error": err}
    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        return {"error": "未找到可行路线"}
    p = paths[0]
    dist_m = float(p.get("distance", 0))
    dur_s = float(p.get("duration", 0))
    return {
        "origin": origin,
        "destination": destination,
        "distance_km": round(dist_m / 1000, 2),
        "duration_min": round(dur_s / 60),
        "tolls": p.get("tolls", ""),
        "traffic_lights": p.get("traffic_lights", ""),
    }


# ---------------------------------------------------------------- 渲染
def render_one(r: dict) -> str:
    out = []
    out.append("-" * 78)
    out.append(f"查询：{r['query']}（{r['city']}）")
    if r.get("error"):
        out.append(f"结果：{r['status']}  {r['error']}")
    else:
        out.append(f"结果：{r['status']}   匹配度 {r['score']}")
        out.append(f"规范名：{r['matched_name']}")
        out.append(f"地址：  {r['district']} {r['address']}")
        out.append(f"电话：  {r['tel'] or '（接口未返回，需另行确认）'}")
        out.append(f"坐标：  {r['location']}   POI: {r['poi_id']}")
        metrics = []
        if r.get("rating"):
            metrics.append(f"评分 {r['rating']:.1f}")
        if r.get("cost"):
            metrics.append(f"人均 ¥{r['cost']:.0f}（可直接回填餐饮科目）")
        if metrics:
            out.append("口碑/消费：" + "   ".join(metrics))
        halls = r.get("halls") or []
        if halls:
            out.append("宴会厅/会议厅子点位（做 L4 承载力核验时优先打这些电话）：")
            for h in halls:
                tel = f"   {h['tel']}" if h["tel"] else ""
                out.append(f"  ▸ {h['name']}{tel}")
        hall_names = {h["name"] for h in halls}
        rest = [
            c for c in r.get("candidates", [])
            if c["name"] != r.get("matched_name") and c["name"] not in hall_names
        ]
        if rest:
            out.append("其他候选：")
            for c in rest[:4]:
                out.append(f"  · {c['name']}（{c['district']}）匹配 {c['score']}")
    out.append("")
    out.append("  ※ 以上仅确认 L1-L3（存在/地址/业态）。容量、报价、档期仍需查官网或致电确认。")
    return "\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="高德 POI 核验工具")
    p.add_argument("--name", help="要核验的点位名称")
    p.add_argument("--city", default="", help="城市名，强烈建议提供（避免全国同名店）")
    p.add_argument("--batch", help="批量文件，每行 名称|城市")
    p.add_argument("--distance", nargs=2, metavar=("ORIGIN", "DEST"),
                   help="两点驾车距离，格式 lng,lat")
    p.add_argument("--key", help="高德 Web 服务 Key，缺省读环境变量 AMAP_KEY")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--interval", type=float, default=1.0,
                   help="批量核验时的请求间隔秒数，默认 1.0（个人 Key QPS 很低，别调太小）")
    p.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（证书链异常时用）")
    args = p.parse_args()

    key, source = resolve_key(args.key)
    if not key:
        sys.stderr.write(
            "没有找到高德 Key。三种配法任选其一：\n"
            "  1) 存文件（推荐，免重启）：把 32 位 Key 写入 ~/.amap_key\n"
            "  2) 环境变量：setx AMAP_KEY \"你的Key\"（要重启 WorkBuddy 才生效）\n"
            "  3) 单次调用：--key \"你的Key\"\n"
            "  Key 类型必须是「Web 服务」，不是「Web 端 JS API」。\n\n"
            "降级方案：改用联网搜索核验，规则见 references/poi-verification.md\n"
            "  查询式 1：\"<酒店全名>\" <城市> 官网\n"
            "  查询式 2：\"<酒店全名>\" <城市> 地址 电话\n"
            "至少两个独立来源地址一致，才可标 ✅；否则标 ⚠️。\n"
            "并在方案中注明「搜索核验」，不得冒充 API 核验。\n"
        )
        sys.exit(2)
    sys.stderr.write(f"Key {mask(key)}（来源：{source}）\n")

    if args.distance:
        try:
            res = check_distance(args.distance[0], args.distance[1], key, args.insecure)
        except Exception as exc:
            sys.stderr.write(f"距离计算失败：{exc}\n")
            sys.exit(1)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif res.get("error"):
            print(f"❌ {res['error']}")
        else:
            print(f"驾车 {res['distance_km']} km，约 {res['duration_min']} 分钟"
                  f"（过路费 {res['tolls'] or '-'} 元，红绿灯 {res['traffic_lights'] or '-'} 个）")
        return

    if args.batch:
        try:
            with open(args.batch, "r", encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        except OSError as exc:
            sys.stderr.write(f"读不到批量文件：{exc}\n")
            sys.exit(1)
        results = []
        for idx, ln in enumerate(lines):
            if idx and args.interval > 0:
                time.sleep(args.interval)
            parts = [x.strip() for x in ln.split("|")]
            nm = parts[0]
            ct = parts[1] if len(parts) > 1 else args.city
            results.append(check_one(nm, ct, key, args.insecure))
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for r in results:
                print(render_one(r))
            ok = sum(1 for r in results if "已核验" in r["status"])
            warn = sum(1 for r in results if "待确认" in r["status"])
            miss = sum(1 for r in results if "查无此店" in r["status"])
            err = len(results) - ok - warn - miss
            line = f"汇总：✅ {ok}   ⚠️ {warn}   ❌ {miss}（查无此店）"
            if err:
                line += f"   ⛔ {err}（接口/网络错误，需重跑）"
            line += f"   共 {len(results)}"
            print("=" * 78)
            print(line)
        sys.exit(0 if any(r["status"][0] in "✅⚠" for r in results) else 3)

    if not args.name:
        p.error("必须提供 --name、--batch 或 --distance 之一")

    r = check_one(args.name, args.city, key, args.insecure)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(render_one(r))
    sys.exit(3 if r["status"].startswith("❌") else 0)


if __name__ == "__main__":
    main()
