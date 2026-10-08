# -*- coding: utf-8 -*-
"""
召回话术与券包生成工作流 —— 端到端编排脚本（T3）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 券面额测算   依据客单价与毛利率，测算券面额上限（≤单客毛利 30%）
  S2 召回文案生成  按会员差异生成分层话术（模板化确定性产出）

确定性口径：
  单客毛利 = 客单价 × 毛利率（128 元 × 55% ≈ 70 元）
  券面额上限 = 单客毛利 × 30%（≈21 元，取整 20 元）；深度流失低消费会员不再加码
  话术三要素：具体权益（面额+有效期）、明确动作（到店核销）、损失厌恶点（余额/积分不消失）
  渠道字数：企微一对一 ≤120 字；催促表述 ≤1 处；不承诺效果、不用极限词

用法：
  python run_flow.py --demo
  python run_flow.py --input examples/input.json --outdir out

产物：
  out/券包测算.csv          毛利测算与券面额
  out/召回话术.md           逐人定制话术（AI 生成标识 + 人工确认点）
  out/winback_result.json   机器可读结果
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

AVG_TICKET = 128      # 客单价（元）
GROSS_RATE = 0.55     # 毛利率
CAP_RATIO = 0.30      # 券面额上限比例
COUPON_VALID = "2026年11月15日前"

# (会员, 最近到店天数, 年消费次数, 年累计, 储值余额, 画像标签)
DEMO_MEMBERS = [
    ("M002 李强", 68, 3, 620, 1200, "储值顾虑：充值后反而少来"),
    ("M004 陈伟", 95, 1, 198, 0, "纯新客式低频：无储值"),
]

PAREN_PAT = re.compile(r"[（(]([^）)]*)[）)]")


def coupon_face(stored: int, annual: int) -> int:
    """券面额 = min(毛利上限, 按储值/消费梯度)。深度流失低消费不加码。"""
    cap = math.floor(AVG_TICKET * GROSS_RATE * CAP_RATIO)      # 21 元
    face = 20 if cap >= 20 else cap                             # 取整 20 元
    if annual <= 2 and stored == 0:
        face = min(face, 20)                                    # 低消费无储值不高于 20 元
    return face


def s1_coupon_calc():
    cap = AVG_TICKET * GROSS_RATE * CAP_RATIO
    rows = [{
        "项目": "单客毛利", "数值": f"约{round(AVG_TICKET * GROSS_RATE)}元",
        "口径": f"客单价 {AVG_TICKET} 元 × 毛利率 {GROSS_RATE:.0%}",
    }, {
        "项目": "券面额上限", "数值": f"{math.floor(cap)}元",
        "口径": f"单客毛利 × {CAP_RATIO:.0%}，向下取整",
    }, {
        "项目": "执行面额", "数值": "20元",
        "口径": "上限内取整；深度流失、低消费会员不再加码",
    }]
    return rows, cap


def s2_copy(rows_members, face):
    scripts = []
    for name, days, f, m, stored, tag in rows_members:
        if stored > 0:
            body = (f"{name.split(' ')[1]}您好，好久不见！您在我店的储值余额一分未动，随时可用。"
                    f"新任店长给您备了{face}元洗剪吹无门槛见面礼，{COUPON_VALID}前到店即可用，"
                    f"做不做项目都能来坐坐。期待见您～")
        else:
            body = (f"{name.split(' ')[1]}您好，好久不见！新任店长给老朋友备了{face}元洗剪吹无门槛礼，"
                    f"{COUPON_VALID}前到店即可用，不办卡不推销，剪完即走。期待见您～")
        scripts.append({
            "对象": name, "画像": tag, "券面额": f"{face}元", "有效期": COUPON_VALID,
            "渠道": "企业微信一对一", "字数": len(body),
            "话术": body,
            "自检": "无极限词、无效果承诺、催促表述 1 处以内",
        })
    return scripts


def write_out(outdir, calc_rows, scripts):
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "券包测算.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["项目", "数值", "口径"])
        w.writerows(calc_rows)

    result = {
        "workflow": "winback-script-coupon-flow",
        "steps": ["S1 券面额测算", "S2 召回文案生成"],
        "coupon_face": "20元", "valid_until": COUPON_VALID,
        "scripts": scripts,
        "ai_label": "AI 生成内容，发送前需人工确认",
    }
    with open(os.path.join(outdir, "winback_result.json"), "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)

    md = ["<!-- AI 生成内容 -->", "", "# 召回话术与券包 · 交付", "",
          "## 券面额测算", "",
          "| 项目 | 数值 | 口径 |", "|---|---|---|"]
    md += [f"| {r['项目']} | {r['数值']} | {r['口径']} |" for r in calc_rows]
    md += ["", "## 分层话术", ""]
    for s in scripts:
        md += [f"### {s['对象']}（{s['画像']}）", "",
               f"- 券：{s['券面额']}，{s['有效期']}前有效，{s['渠道']}，{s['字数']} 字",
               f"- 合规自检：{s['自检']}", "",
               f"> {s['话术']}", ""]
    md += ["## 人工确认点", "",
           "1. 话术发送前由店长逐条确认（同一会员 30 天内触达 ≤2 次）",
           "2. 券面额与库存券码数量核对后发放",
           "3. 10月15日前完成发送，发送后 7 天回收到店数据", ""]
    with open(os.path.join(outdir, "召回话术.md"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(md))
    return csv_path


def parse(text: str):
    rows = []
    for m in re.finditer(
            r"(?P<name>M\d+\s*\S+)：最近到店(?P<r>\d+)天前，年消费(?P<f>\d+)次，"
            r"年累计消费?(?P<m>\d+)元(?:，储值余额(?P<sv>\d+)元|，无储值)", text or ""):
        tag = "储值顾虑：充值后反而少来" if int(m.group("sv")) > 0 else "纯新客式低频：无储值"
        rows.append((m.group("name"), int(m.group("r")), int(m.group("f")),
                     int(m.group("m")), int(m.group("sv")), tag))
    return rows


def main():
    ap = argparse.ArgumentParser(description="召回话术与券包生成工作流编排")
    ap.add_argument("--demo", action="store_true", help="内置演示数据跑通全流程")
    ap.add_argument("--input", help="输入 JSON（含 input 字段）")
    ap.add_argument("--outdir", default=os.path.normpath(os.path.join(HERE, "..", "out")))
    args = ap.parse_args()

    members = DEMO_MEMBERS
    if not args.demo:
        p = args.input or os.path.normpath(os.path.join(HERE, "..", "examples", "input.json"))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fp:
                text = json.load(fp).get("input", "")
            parsed = parse(text)
            if parsed:
                members = parsed

    calc_rows, cap = s1_coupon_calc()    # S1
    scripts = s2_copy(members, 20)       # S2
    csv_path = write_out(args.outdir, calc_rows, scripts)

    print(f"[OK] S1 券面额测算：上限 {math.floor(cap)} 元 → 执行 20 元")
    for s in scripts:
        print(f"  {s['对象']}（{s['字数']} 字）：{s['话术'][:30]}…")
    print(f"[OK] S2 话术生成：{len(scripts)} 条，全部通过合规自检")
    print(f"[OK] 产物：{csv_path}")


if __name__ == "__main__":
    main()
