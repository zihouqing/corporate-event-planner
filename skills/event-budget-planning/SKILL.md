---
name: event-budget-planning
description: "Budget reverse-engineering and venue verification for corporate events (team building, annual galas, client appreciation, training). Use when you must allocate a fixed budget across cost categories, decide per-head tiers, check venue capacity against headcount, or verify that a hotel/venue actually exists via AMap POI or web search."
---

# 企业活动预算与点位核验

给「老周」用的两套硬工具：一套算钱，一套验店。两件事都做完了，方案才算成立。

## 何时使用本技能

- 用户给出预算总额，要拆科目、定人均档位
- 需要判断「这个预算能不能办成这个规模的活动」
- 需要生成酒店/场地候选并核验真实性
- 需要校验宴会厅面积、客房数、车辆数是否够用
- 已有方案要复核预算和平替点位

## 三件事，按顺序做

> **脚本位置**：下面两个脚本都在本技能的 `scripts/` 目录下。执行时优先用技能根目录的完整路径（`python3 <skill-root>/scripts/budget_calc.py ...`）；只有在 `scripts/` 目录内时才可直接写 `python3 budget_calc.py`。不要在未知工作目录下直接敲相对路径，会报文件不存在。

### 1. 算钱 → `scripts/budget_calc.py`

```bash
python3 scripts/budget_calc.py --total 80000 --people 30 --days 2 --type teambuilding --city-tier 1
```

必填：`--total`（总额）、`--people`（人数）、`--days`（天数）、`--type`（teambuilding / annual / client / training）
选填：`--city-tier`（1/2/3/4，默认 2）、`--reserve`（机动金率，覆盖默认）、`--per-head-cap`（反向模式：给人均上限反推总额）

输出：科目分配表 + 人均日成本 + 档位判定 + 承载力校验。计算逻辑见 `references/budget-model.md`。

### 2. 验店 → `scripts/poi_check.py`

```bash
# 单点核验
python3 scripts/poi_check.py --name "深圳湾万丽酒店" --city 深圳

# 批量核验（每行一个「名称|城市」）
python3 scripts/poi_check.py --batch venues.txt

# 两点驾车距离
python3 scripts/poi_check.py --distance "113.93,22.52" "114.06,22.54"
```

输出包含：规范名、地址、电话、坐标、POI 编号、**评分**、**餐饮人均消费**，以及该酒店自身的**宴会厅子点位**（L4 承载力线索）。

两个使用要点：

- **批量核验默认每次间隔 1 秒**。个人 Key 的 QPS 很低，连续请求会撞 `10021`。脚本已内置退避重试，别把 `--interval` 调到 1 以下。
- **餐饮人均能拿到，酒店房价拿不到**。`cost` 字段只有餐饮类有值，酒店返回空。所以餐饮科目可以直接回填真实值，住宿科目必须标注"需询价"。

需要高德**「Web 服务」**类型的 Key（不是「Web 端 JS API」）。查找顺序：`--key` 参数 → 环境变量 `AMAP_KEY` → 密钥文件 `~/.amap_key` 或 `~/.workbuddy/amap_key` → 可选的随包 `amap_key.txt`。

**本仓库不含任何 Key**。使用前请自行申请并配置：把 32 位 Key 单独一行写进 `~/.amap_key`（无引号、无空格），不用改环境变量、不用重启 WorkBuddy。

申请入口：<https://console.amap.com> → 应用管理 → 创建应用 → 添加 Key → 服务平台必须选「Web 服务」。

**没有 Key 怎么办**：脚本会以退出码 2 结束并提示降级。此时改用联网搜索核验，规则见 `references/poi-verification.md`。降级不算失败，但**必须**在方案里标注"搜索核验"，不能冒充 API 核验。

### 3. 对平 → 人工判断

用真实价格回填后重算总额，偏差 >5% 就调科目，不许宣布"基本符合"。承载力校验不通过的场地直接淘汰，不许"挤一挤"。

## 核验三态（强制）

| 标记 | 条件 | 能否进正文 |
|---|---|---|
| ✅ 已核验 | 名称 + 地址 + 营业状态全对得上 | 可以 |
| ⚠️ 待确认 | 主体存在，容量/报价/档期未证实 | 可以，但必须进「待确认清单」 |
| ❌ 查无此店 | 搜索无果 / 名称对不上 / 已停业 | **不可以**，只能进「已排除」附注 |

## 参考文件

- `references/budget-model.md` — 人均档位、科目配比、城市系数、承载力公式、常见超支陷阱
- `references/poi-verification.md` — 核验流程、API 字段、降级搜索法、误判排查、输出模板

## 三条纪律

1. 数据是**经验区间**，不是报价承诺。输出时必须写"参考价，以销售正式报价为准"。
2. 脚本算的是**骨架**，不是终值。真实价格一定要回填重算。
3. 任何点位在过核验之前，一律标"未核验"，不许先写进正文再补。
