# -*- coding: utf-8 -*-
"""
会员 RFM 分层工作流 —— 端到端编排脚本（T3）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 会员数据对接   解析系统导出的会员消费记录，产出字段映射与清洗后的结构化数据
  S2 RFM 分层       按 R 40% / F 30% / M 30% 加权打分，切分四层

口径（与 rfm-segmentation 技能一致）：
  R 最近到店间隔：≤15 天=5，16-30=4，31-60=3，61-90=2，>90=1
  F 年消费次数：≥10=5，7-9=4，4-6=3，2-3=2，≤1=1
  M 年累计消费：≥3000 元=5，2000-2999=4，1000-1999=3，500-999=2，<500=1
  分层：≥4.5 高价值；3.5-4.4 潜力；2.5-3.4 一般挽留；<2.5 流失风险

用法：
  python run_flow.py --demo
  python run_flow.py --input examples/input.json --outdir out

产物：
  out/字段映射.csv          S1 产物：业务字段 ↔ 系统字段
  out/rfm_flow_scores.csv   S2 产物：逐条评分明细
  out/rfm_flow_result.json  机器可读结果
  out/执行摘要.md           工作流执行摘要（AI 生成标识）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))

EXPORT_DATE = date(2026, 10, 7)
WEIGHTS = {"R": 0.4, "F": 0.3, "M": 0.3}

DEMO_TEXT = (
    "来源：有赞连锁门店会员系统（收银+会员模块）后台导出，导出日期2026-10-07。"
    "任务：将近一年会员消费记录接入并完成RFM四层分桶（高价值/潜力/一般挽留/流失风险）。导出的5条会员记录：\n"
    "1. 会员M001 王芳：手机号138****2351，最近到店2026-10-02，年消费12次，年累计消费3860元，储值余额520元\n"
    "2. 会员M002 李强：手机号159****7702，最近到店2026-07-31，年消费3次，年累计消费620元，无储值\n"
    "3. 会员M003 张敏：手机号136****4408，最近到店2026-09-23，年消费8次，年累计消费2450元，储值余额800元\n"
    "4. 会员M004 陈伟：手机号187****9913，最近到店2026-07-05，年消费1次，年累计消费198元，无储值\n"
    "5. 会员M005 刘洋：手机号150****3367，最近到店2026-08-29，年消费5次，年累计消费1180元，储值余额150元"
)

FIELD_MAP = [
    ["会员ID", "有赞会员 member_id", "读取", "主键，用于打标对齐"],
    ["手机号", "有赞会员 mobile", "读取", "已脱敏（中间 4 位打码），仅作触达联系方式"],
    ["最近到店时间", "交易订单 created_at（已完成订单 MAX）", "读取", "剔除退款订单"],
    ["年消费次数", "近 1 年已完成订单按 member_id 计数", "读取", "剔除退款单"],
    ["年累计消费金额", "近 1 年 sum(pay_amount)", "读取", "实付口径，不含运费与充值流水"],
    ["储值余额", "储值账户 balance", "读取", "当前可用余额，非累计充值"],
    ["RFM 分层标签", "会员标签组「会员分层」", "写回", "高价值/潜力/一般挽留/流失风险"],
]

REC_PAT = re.compile(
    r"(?P<name>会员M\d+\s*\S+?)：手机号(?P<phone>[\d*]+)，最近到店(?P<date>\d{4}-\d{2}-\d{2})，"
    r"年消费(?P<f>\d+)次，年累计消费(?P<m>\d+)元，(?:储值余额(?P<sv>\d+)元|无储值)"
)


def score(days: int, times: int, amount: int):
    r = 5 if days <= 15 else 4 if days <= 30 else 3 if days <= 60 else 2 if days <= 90 else 1
    f = 5 if times >= 10 else 4 if times >= 7 else 3 if times >= 4 else 2 if times >= 2 else 1
    m = 5 if amount >= 3000 else 4 if amount >= 2000 else 3 if amount >= 1000 else 2 if amount >= 500 else 1
    return r, f, m


def segment(total: float) -> str:
    if total >= 4.5:
        return "高价值会员"
    if total >= 3.5:
        return "潜力会员"
    if total >= 2.5:
        return "一般挽留会员"
    return "流失风险会员"


def s1_data_sync(text: str):
    """S1 会员数据对接：解析导出记录 → 结构化行 + 字段映射。"""
    rows = []
    for m in REC_PAT.finditer(text):
        last = date.fromisoformat(m.group("date"))
        rows.append({
            "name": m.group("name"),
            "phone": m.group("phone"),
            "last_visit": last,
            "r_days": (EXPORT_DATE - last).days,
            "f_times": int(m.group("f")),
            "m_amount": int(m.group("m")),
            "stored": int(m.group("sv")) if m.group("sv") else 0,
        })
    return rows


def s2_rfm(rows):
    """S2 RFM 分层：逐条打分与四层分桶。"""
    details = []
    for i, r0 in enumerate(rows, 1):
        r, f, m = score(r0["r_days"], r0["f_times"], r0["m_amount"])
        total = round(r * WEIGHTS["R"] + f * WEIGHTS["F"] + m * WEIGHTS["M"], 1)
        details.append({
            "#": str(i), "对象": r0["name"],
            "R分": f"{r}（{r0['r_days']}天）", "F分": f"{f}（{r0['f_times']}次）",
            "M分": f"{m}（{r0['m_amount']}元）",
            "总分": f"{total}", "分层": segment(total),
        })
    return details


def write_out(outdir, details):
    os.makedirs(outdir, exist_ok=True)
    fm_path = os.path.join(outdir, "字段映射.csv")
    with open(fm_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["业务字段", "系统字段", "方向", "说明"])
        w.writerows(FIELD_MAP)

    sc_path = os.path.join(outdir, "rfm_flow_scores.csv")
    with open(sc_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(details[0].keys()))
        w.writeheader()
        w.writerows(details)

    buckets = {}
    for d in details:
        buckets.setdefault(d["分层"], []).append(d["对象"])
    n = len(details) or 1
    summary_rows = [
        {"层级": k, "数量": str(len(v)), "占比": f"{round(len(v)/n*100)}%", "会员": "、".join(v)}
        for k, v in buckets.items()
    ]
    result = {
        "workflow": "member-rfm-segmentation-flow",
        "steps": ["S1 会员数据对接", "S2 RFM 分层"],
        "weights": WEIGHTS,
        "count": len(details),
        "summary": summary_rows,
        "details": details,
        "ai_label": "AI 生成内容，外发前需人工审核",
    }
    rs_path = os.path.join(outdir, "rfm_flow_result.json")
    with open(rs_path, "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)

    md = ["<!-- AI 生成内容 -->", "", "# 会员 RFM 分层工作流 · 执行摘要", "",
          f"- S1 会员数据对接：解析 {len(rows_d)} 条导出记录（导出日 {EXPORT_DATE}），字段映射 7 项",
          f"- S2 RFM 分层：按 R 40% / F 30% / M 30% 加权，四层分桶结果如下", "",
          "| 层级 | 数量 | 占比 | 会员 |", "|---|---|---|---|"]
    md += [f"| {r['层级']} | {r['数量']} | {r['占比']} | {r['会员']} |" for r in summary_rows]
    md += ["", "> 人工确认点：打标前抽样核对 5-10 名会员，一致率须为 100%。", ""]
    with open(os.path.join(outdir, "执行摘要.md"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(md))
    return fm_path, sc_path, rs_path


rows_d = []


def main():
    global rows_d
    ap = argparse.ArgumentParser(description="会员 RFM 分层工作流编排")
    ap.add_argument("--demo", action="store_true", help="内置演示数据跑通全流程")
    ap.add_argument("--input", help="输入 JSON（含 input 字段）")
    ap.add_argument("--outdir", default=os.path.normpath(os.path.join(HERE, "..", "out")))
    args = ap.parse_args()

    text = DEMO_TEXT
    if not args.demo:
        p = args.input or os.path.normpath(os.path.join(HERE, "..", "examples", "input.json"))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fp:
                text = json.load(fp).get("input", DEMO_TEXT)

    rows_d = s1_data_sync(text)          # S1
    if not rows_d:
        print("[错误] S1 未解析到任何会员记录", file=sys.stderr)
        sys.exit(1)
    details = s2_rfm(rows_d)             # S2
    paths = write_out(args.outdir, details)

    print(f"[OK] S1 会员数据对接：解析 {len(rows_d)} 条记录，字段映射 {len(FIELD_MAP)} 项")
    for d in details:
        print(f"  {d['对象']}：总分 {d['总分']} → {d['分层']}")
    print(f"[OK] S2 RFM 分层：完成四层分桶")
    for p in paths:
        print(f"[OK] 产物：{p}")


if __name__ == "__main__":
    main()
