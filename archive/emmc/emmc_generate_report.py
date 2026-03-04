#!/usr/bin/env python3
"""
eMMC 健康状态 HTML 分析报告生成器

读取 all_results.json，生成自包含的 HTML 分析报告。
重点关注 EST_TYP_A（eMMC 磨损程度），按厂家（devName）分维度分析。
"""

import json
import html as html_mod
import os
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "emmc_results")
INPUT_FILE = os.path.join(RESULTS_DIR, "all_results.json")
OUTPUT_FILE = os.path.join(RESULTS_DIR, "emmc_report.html")

# ── 健康等级定义 ──────────────────────────────────────────────
HEALTH_LEVELS = [
    {"name": "健康", "min": 1, "max": 3, "color": "#22c55e", "bg": "#f0fdf4", "border": "#bbf7d0"},
    {"name": "良好", "min": 4, "max": 6, "color": "#f59e0b", "bg": "#fefce8", "border": "#fef08a"},
    {"name": "警告", "min": 7, "max": 9, "color": "#f97316", "bg": "#fff7ed", "border": "#fed7aa"},
    {"name": "危险", "min": 10, "max": 11, "color": "#ef4444", "bg": "#fef2f2", "border": "#fecaca"},
]


def parse_hex(val: str) -> int:
    """将 0x0b 格式转为十进制整数。"""
    try:
        return int(val, 16)
    except (ValueError, TypeError):
        return -1


def health_level(val: int):
    """根据 EST_TYP_A 数值返回健康等级信息。"""
    for lv in HEALTH_LEVELS:
        if lv["min"] <= val <= lv["max"]:
            return lv
    return HEALTH_LEVELS[-1]


def bar_color(val: int) -> str:
    """柱状图中每个 EST_TYP_A 值对应的颜色。"""
    return health_level(val)["color"]


def generate():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    total = len(data)
    online_count = sum(1 for d in data if d.get("status") == "online")

    # ── 数据分类：失败 vs 有效 ────────────────────────────
    failed_devices = []
    valid_data = []
    for d in data:
        a = parse_hex(d.get("EST_TYP_A", ""))
        b = parse_hex(d.get("EST_TYP_B", ""))
        if a <= 0 and b <= 0:
            failed_devices.append(d)
        else:
            valid_data.append(d)

    failed_count = len(failed_devices)
    valid_count = len(valid_data)

    # ── 基础统计（仅基于有效数据）──────────────────────────
    dev_names = sorted(set(d.get("devName", "N/A") for d in valid_data)) if valid_data else []
    vendor_count = len(dev_names)

    # EST_TYP_A 整体分布（仅有效数据）
    typ_a_counter = Counter()
    for d in valid_data:
        v = parse_hex(d.get("EST_TYP_A", "0x00"))
        if v > 0:
            typ_a_counter[v] += 1

    all_typ_a_vals = sorted(typ_a_counter.keys())

    # 健康等级统计
    level_counts = {lv["name"]: 0 for lv in HEALTH_LEVELS}
    for val, cnt in typ_a_counter.items():
        lv = health_level(val)
        level_counts[lv["name"]] += cnt

    # ── 按厂家分析（仅有效数据）──────────────────────────
    vendor_typ_a = defaultdict(Counter)
    vendor_total = Counter()
    vendor_sum = defaultdict(int)

    for d in valid_data:
        dev = d.get("devName", "N/A")
        v = parse_hex(d.get("EST_TYP_A", "0x00"))
        if v > 0:
            vendor_typ_a[dev][v] += 1
            vendor_total[dev] += 1
            vendor_sum[dev] += v

    vendor_avg = {dev: vendor_sum[dev] / vendor_total[dev] if vendor_total[dev] else 0 for dev in dev_names}

    # 厂家健康等级占比
    vendor_level_counts = {}
    for dev in dev_names:
        vendor_level_counts[dev] = {lv["name"]: 0 for lv in HEALTH_LEVELS}
        for val, cnt in vendor_typ_a[dev].items():
            lv = health_level(val)
            vendor_level_counts[dev][lv["name"]] += cnt

    # ── 风险网关（EST_TYP_A >= 7，仅有效数据）──────────────
    risk_devices = []
    for d in valid_data:
        v = parse_hex(d.get("EST_TYP_A", "0x00"))
        if v >= 7:
            risk_devices.append({**d, "_typ_a_dec": v})
    risk_devices.sort(key=lambda x: x["_typ_a_dec"], reverse=True)

    # ── JSON 数据序列化 ──────────────────────────────────
    data_json = json.dumps(data, ensure_ascii=False)
    risk_json = json.dumps(risk_devices, ensure_ascii=False)
    failed_json = json.dumps(failed_devices, ensure_ascii=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 图表数据准备 ─────────────────────────────────────
    dist_labels = [f"0x{v:02x}" for v in all_typ_a_vals]
    dist_values = [typ_a_counter[v] for v in all_typ_a_vals]
    dist_colors = [bar_color(v) for v in all_typ_a_vals]

    vendor_colors = ["#3b82f6", "#8b5cf6", "#ec4899"]
    vendor_borders = ["#2563eb", "#7c3aed", "#db2777"]
    grouped_datasets_js = ""
    for i, dev in enumerate(dev_names):
        vals = [vendor_typ_a[dev].get(v, None) for v in all_typ_a_vals]
        vals_json = json.dumps(vals)
        grouped_datasets_js += f"""{{
            label: '{dev}',
            data: {vals_json},
            backgroundColor: '{vendor_colors[i % len(vendor_colors)]}',
            borderColor: '{vendor_borders[i % len(vendor_borders)]}',
            borderWidth: 1,
            borderRadius: 4,
            skipNull: true,
        }},\n"""

    pie_labels = json.dumps(dev_names)
    pie_values = json.dumps([vendor_total[d] for d in dev_names])
    pie_colors = json.dumps(vendor_colors[:len(dev_names)])

    avg_labels = json.dumps(dev_names)
    avg_values = json.dumps([round(vendor_avg[d], 2) for d in dev_names])
    avg_colors = json.dumps([bar_color(round(vendor_avg[d])) for d in dev_names])

    stacked_datasets_js = ""
    for lv in HEALTH_LEVELS:
        vals = [vendor_level_counts[dev][lv["name"]] for dev in dev_names]
        stacked_datasets_js += f"""{{
            label: '{lv["name"]}',
            data: {json.dumps(vals)},
            backgroundColor: '{lv["color"]}',
        }},\n"""

    # ── 概览卡片 ─────────────────────────────────────────
    level_i18n_keys = ["lvHealthy", "lvGood", "lvWarning", "lvDanger"]
    cards_html = ""
    for i, lv in enumerate(HEALTH_LEVELS):
        cnt = level_counts[lv["name"]]
        pct = round(cnt / valid_count * 100, 1) if valid_count else 0
        cards_html += f"""
        <div class="health-card" style="--accent:{lv['color']}">
            <div class="hc-label" data-i18n="{level_i18n_keys[i]}">{lv['name']}</div>
            <div class="hc-value" style="color:{lv['color']}">{cnt}</div>
            <div class="hc-sub">{pct}% &middot; EST_TYP_A {lv['min']}~{lv['max']}</div>
        </div>\n"""

    # ── 图标 & Favicon ───────────────────────────────────
    icon_svg = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: text-bottom; margin-right: 8px; color: #3b82f6;"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect><rect x="9" y="9" width="6" height="6"></rect><line x1="9" y1="1" x2="9" y2="4"></line><line x1="15" y1="1" x2="15" y2="4"></line><line x1="9" y1="20" x2="9" y2="23"></line><line x1="15" y1="20" x2="15" y2="23"></line><line x1="20" y1="9" x2="23" y2="9"></line><line x1="20" y1="14" x2="23" y2="14"></line><line x1="1" y1="9" x2="4" y2="9"></line><line x1="1" y1="14" x2="4" y2="14"></line></svg>"""
    favicon_svg_content = icon_svg.replace('currentColor', '#3b82f6').replace('style="vertical-align: text-bottom; margin-right: 8px; color: #3b82f6;"', '')
    favicon_href = "data:image/svg+xml," + urllib.parse.quote(favicon_svg_content)

    esc = html_mod.escape

    # ── HTML 模板 ─────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" href="{favicon_href}" type="image/svg+xml">
<title>eMMC 健康状态分析报告</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root {{
  --bg:#0f172a; --text:#e2e8f0; --text-muted:#94a3b8; --text-dim:#475569;
  --card-bg:rgba(255,255,255,0.04); --border:rgba(255,255,255,0.08);
  --input-bg:rgba(255,255,255,0.05); --input-border:rgba(255,255,255,0.12);
  --table-hover:rgba(255,255,255,0.03); --table-header:#192134;
  --btn-bg:rgba(30,41,59,0.9); --btn-hover:rgba(51,65,85,0.9);
  --shadow:rgba(0,0,0,0.4); --bar-bg:rgba(255,255,255,0.06);
  --health-good:#22c55e; --health-warn:#f59e0b; --health-bad:#ef4444;
  --chart-text:#94a3b8; --chart-grid:rgba(255,255,255,0.06);
  --link-color:#60a5fa;
}}
:root.light {{
  --bg:#f8fafc; --text:#1e293b; --text-muted:#64748b; --text-dim:#94a3b8;
  --card-bg:rgba(0,0,0,0.03); --border:rgba(0,0,0,0.08);
  --input-bg:rgba(0,0,0,0.03); --input-border:rgba(0,0,0,0.15);
  --table-hover:rgba(0,0,0,0.03); --table-header:#eef0f2;
  --btn-bg:rgba(255,255,255,0.9); --btn-hover:rgba(230,230,230,0.9);
  --shadow:rgba(0,0,0,0.12); --bar-bg:rgba(0,0,0,0.06);
  --chart-text:#64748b; --chart-grid:rgba(0,0,0,0.08);
  --link-color:#2563eb;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
::-webkit-scrollbar {{ width:6px; height:6px; }}
::-webkit-scrollbar-track {{ background:transparent; }}
::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,0.15); border-radius:3px; }}
::-webkit-scrollbar-thumb:hover {{ background:rgba(255,255,255,0.3); }}
:root.light ::-webkit-scrollbar-thumb {{ background:rgba(0,0,0,0.15); }}
:root.light ::-webkit-scrollbar-thumb:hover {{ background:rgba(0,0,0,0.3); }}
* {{ scrollbar-width:thin; scrollbar-color:rgba(255,255,255,0.15) transparent; }}
:root.light * {{ scrollbar-color:rgba(0,0,0,0.15) transparent; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--text); line-height:1.6; transition:background 0.3s,color 0.3s; }}
.container {{ max-width:1280px; margin:0 auto; padding:24px; }}
.top-bar {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:16px; }}
.top-bar h1 {{ font-size:15px; font-weight:700; white-space:nowrap; }}
.tb-right {{ display:flex; align-items:center; gap:8px; }}
.lang-group {{ display:flex; gap:2px; }}
.lang-btn {{
  padding:4px 10px; border-radius:12px; border:1.5px solid var(--input-border);
  background:transparent; color:var(--text-muted); font-size:11px; font-weight:600;
  cursor:pointer; transition:all 0.2s;
}}
.lang-btn.active {{ background:#3b82f6; border-color:#3b82f6; color:#fff; }}
.lang-btn:hover:not(.active) {{ background:var(--btn-hover); border-color:#3b82f6; }}
.theme-btn {{
  width:28px; height:28px; border-radius:50%; border:1.5px solid var(--input-border);
  background:var(--input-bg); color:var(--text-muted); font-size:14px;
  cursor:pointer; display:flex; align-items:center; justify-content:center;
  transition:all 0.2s; flex-shrink:0;
}}
.theme-btn:hover {{ background:var(--btn-hover); border-color:#3b82f6; }}
.report-meta {{
  display:flex; gap:24px; flex-wrap:wrap; font-size:12px; color:var(--text-muted);
  margin-bottom:20px; padding:10px 14px; background:var(--card-bg);
  border:1px solid var(--border); border-radius:8px;
}}
.report-meta b {{ color:var(--text); font-weight:600; }}
.section-title {{
  font-size:14px; font-weight:700; margin:0; padding:20px 0 10px 0; color:var(--text-muted);
}}
.ov-cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:24px; }}
.ov-card {{
  background:var(--card-bg); border-radius:12px; padding:16px; text-align:center;
  border:1px solid var(--border);
}}
.ov-card .ov-label {{ font-size:11px; color:var(--text-muted); margin-bottom:4px; }}
.ov-card .ov-value {{ font-size:24px; font-weight:700; }}
.ov-card .ov-sub {{ font-size:11px; color:var(--text-dim); }}
.health-cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:24px; }}
.health-card {{
  background:var(--card-bg); border-radius:12px; padding:16px;
  border:1px solid var(--border); border-left:4px solid var(--accent);
}}
.health-card .hc-label {{ font-size:11px; color:var(--text-muted); font-weight:500; }}
.health-card .hc-value {{ font-size:28px; font-weight:700; margin:4px 0; }}
.health-card .hc-sub {{ font-size:11px; color:var(--text-dim); }}
.charts-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }}
.chart-box {{
  background:var(--card-bg); border-radius:12px; padding:16px;
  border:1px solid var(--border);
}}
.chart-box.full {{ grid-column:1 / -1; }}
.chart-box h3 {{ font-size:12px; font-weight:600; color:var(--text-muted); margin:0 0 10px 0; }}
.chart-box canvas {{ max-height:280px; }}
.mac-link {{ color:var(--link-color); text-decoration:underline; cursor:pointer; }}
.mac-link:hover {{ opacity:0.8; }}
.img-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.35); z-index:9998; }}
.img-overlay.show {{ display:block; }}
.img-modal {{
  display:none; position:fixed; z-index:9999;
  background:var(--card-bg); border:1px solid var(--border);
  border-radius:12px; box-shadow:0 8px 32px var(--shadow);
  backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
  min-width:480px; max-width:96vw; width:1100px; overflow:hidden;
}}
.img-modal.show {{ display:block; }}
.img-modal-header {{
  display:flex; align-items:center; justify-content:space-between;
  padding:8px 12px; cursor:move; user-select:none;
  background:var(--table-header); border-bottom:1px solid var(--border);
}}
.img-modal-header span {{ font-size:12px; font-weight:600; color:var(--text); }}
.img-modal-close {{
  width:22px; height:22px; border-radius:50%; border:none;
  background:rgba(239,68,68,0.15); color:#ef4444; font-size:14px;
  cursor:pointer; display:flex; align-items:center; justify-content:center;
  transition:background 0.2s;
}}
.img-modal-close:hover {{ background:rgba(239,68,68,0.3); }}
.img-modal-body {{ padding:12px; text-align:center; max-height:85vh; overflow:auto; }}
.img-modal-body img {{ max-width:100%; border-radius:6px; }}
.img-modal-body .img-error {{ color:var(--text-muted); font-size:12px; padding:32px; }}
.img-modal-actions {{
  display:flex; justify-content:center; gap:8px; padding:0 10px 10px;
}}
.img-modal-actions button {{
  padding:3px 12px; border-radius:12px; border:1px solid var(--input-border);
  background:var(--input-bg); color:var(--text-muted); font-size:11px;
  cursor:pointer; transition:all 0.2s;
}}
.img-modal-actions button:hover {{ background:var(--btn-hover); border-color:#3b82f6; }}
tr.row-highlight {{ background:rgba(59,130,246,0.18) !important; }}
:root.light tr.row-highlight {{ background:rgba(59,130,246,0.12) !important; }}
.table-wrap {{
  background:var(--card-bg); border-radius:12px; padding:16px;
  border:1px solid var(--border); overflow-x:auto; margin-bottom:20px;
}}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th {{
  text-align:left; padding:8px 10px; background:var(--table-header);
  color:var(--text-muted); font-weight:600; position:sticky; top:0; z-index:1;
  cursor:pointer; white-space:nowrap; user-select:none;
}}
th:hover {{ color:var(--text); }}
th .sort-arrow {{ font-size:10px; margin-left:2px; opacity:0.6; }}
.th-tip {{
  display:inline-flex; align-items:center; justify-content:center;
  width:14px; height:14px; border-radius:50%; margin-left:3px; vertical-align:middle;
  font-size:10px; font-weight:700; font-style:normal; font-family:serif;
  color:var(--text-dim); border:1.5px solid var(--text-dim);
  cursor:help; position:relative;
}}
.th-tip:hover {{ color:#3b82f6; border-color:#3b82f6; }}
.th-tip:hover::after {{
  content:attr(data-tip);
  position:absolute; left:50%; top:calc(100% + 6px); transform:translateX(-50%);
  background:rgba(15,23,42,0.95); color:#e2e8f0; font-size:11px; font-weight:400;
  padding:8px 12px; border-radius:8px; white-space:pre-line; line-height:1.6;
  min-width:260px; max-width:360px; z-index:100; pointer-events:none;
  box-shadow:0 4px 16px rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1);
}}
:root.light .th-tip:hover::after {{
  background:rgba(30,41,59,0.95); color:#f1f5f9;
}}
td {{
  padding:6px 10px; border-bottom:1px solid var(--border);
  font-family:"SF Mono","Fira Code",monospace; font-size:11px; white-space:nowrap;
}}
tr:hover td {{ background:var(--table-hover); }}
.mono {{ font-family:"SF Mono","Fira Code",monospace; font-size:11px; }}
.badge {{
  display:inline-block; padding:2px 8px; border-radius:10px;
  font-size:10px; font-weight:700; color:#fff;
}}
.health-badge-good {{ background:#22c55e; }}
.health-badge-warn {{ background:#f59e0b; }}
.health-badge-alert {{ background:#f97316; }}
.health-badge-bad {{ background:#ef4444; }}
.search-bar {{ margin-bottom:12px; }}
.search-bar input {{
  width:100%; max-width:400px; padding:5px 12px; border-radius:16px;
  border:1.5px solid var(--input-border); background:var(--input-bg);
  color:var(--text); font-size:11px; outline:none; transition:all 0.2s;
}}
.search-bar input::placeholder {{ color:var(--text-dim); }}
.search-bar input:focus {{ border-color:#3b82f6; }}
@media (max-width:700px) {{
  .charts-grid {{ grid-template-columns:1fr; }}
  .health-cards {{ grid-template-columns:repeat(2,1fr); }}
  .ov-cards {{ grid-template-columns:repeat(2,1fr); }}
}}
@media print {{
  body {{ background:#fff !important; color:#1e293b !important; }}
  .chart-box, .ov-card, .health-card, .table-wrap {{ border:1px solid #e2e8f0; background:#fff; }}
  .theme-btn, .lang-group {{ display:none; }}
}}
</style>
</head>
<body>
<div class="container">

<!-- 顶部栏 -->
<div class="top-bar">
  <h1>{icon_svg}<span data-i18n="title">eMMC 健康状态分析报告</span></h1>
  <div class="tb-right">
    <div class="lang-group">
      <button class="lang-btn active" onclick="switchLang('zh')">中</button>
      <button class="lang-btn" onclick="switchLang('en')">EN</button>
      <button class="lang-btn" onclick="switchLang('ja')">JP</button>
    </div>
    <button class="theme-btn" onclick="toggleTheme()" title="切换主题">&#9790;</button>
  </div>
</div>
<div class="report-meta">
  <span><span data-i18n="metaGenTime">生成时间</span>: <b>{now}</b></span>
  <span><span data-i18n="metaSource">数据来源</span>: <b>all_results.json</b></span>
  <span><span data-i18n="metaTotal">网关总数</span>: <b>{total}</b></span>
  <span><span data-i18n="metaOnline">在线</span>: <b>{online_count}</b></span>
  <span><span data-i18n="metaVendors">eMMC 厂家</span>: <b>{vendor_count}</b></span>
</div>

<!-- 基础统计 -->
<div class="ov-cards">
  <div class="ov-card"><div class="ov-label" data-i18n="cardTotal">网关总数</div><div class="ov-value" style="color:#3b82f6">{total}</div></div>
  <div class="ov-card"><div class="ov-label" data-i18n="cardOnline">在线网关</div><div class="ov-value" style="color:#22c55e">{online_count}</div></div>
  <div class="ov-card"><div class="ov-label" data-i18n="cardVendors">eMMC 厂家</div><div class="ov-value" style="color:#8b5cf6">{vendor_count}</div></div>
  <div class="ov-card"><div class="ov-label" data-i18n="cardRisk">风险网关 (&ge;0x07)</div><div class="ov-value" style="color:#ef4444">{len(risk_devices)}</div></div>
  <div class="ov-card"><div class="ov-label" data-i18n="cardFailed">检测失败</div><div class="ov-value" style="color:#94a3b8">{failed_count}</div></div>
</div>

<!-- 健康等级概览 -->
<div class="section-title" data-i18n="secHealth">健康等级概览 — 基于 EST_TYP_A 值分级，值越小越健康</div>
<div class="health-cards">
{cards_html}
</div>

<!-- EST_TYP_A 整体分布 -->
<div class="section-title" data-i18n="secDist">EST_TYP_A 整体分布</div>
<div class="charts-grid">
  <div class="chart-box full">
    <h3 data-i18n="chartDistTitle">网关数量 vs EST_TYP_A 值</h3>
    <canvas id="chartDist" height="50"></canvas>
  </div>
</div>

<!-- 按厂家分析 -->
<div class="section-title" data-i18n="secVendor">按厂家 (devName) 分析</div>
<div class="charts-grid">
  <div class="chart-box">
    <h3 data-i18n="chartPieTitle">各厂家网关占比</h3>
    <canvas id="chartPie"></canvas>
  </div>
  <div class="chart-box">
    <h3 data-i18n="chartAvgTitle">各厂家平均 EST_TYP_A</h3>
    <canvas id="chartAvg"></canvas>
  </div>
  <div class="chart-box full">
    <h3 data-i18n="chartGroupTitle">各厂家 EST_TYP_A 分布对比</h3>
    <canvas id="chartGrouped" height="50"></canvas>
  </div>
  <div class="chart-box full">
    <h3 data-i18n="chartStackTitle">各厂家健康等级占比</h3>
    <canvas id="chartStacked" height="40"></canvas>
  </div>
</div>

<!-- 风险网关清单 -->
<div class="section-title"><span data-i18n="secRisk">风险网关清单</span> <span style="font-size:11px;color:#ef4444;font-weight:400;">(EST_TYP_A &ge; 0x07, <span data-i18n="riskCount">共 {len(risk_devices)} 台</span>)</span></div>
<div class="table-wrap">
<div class="search-bar"><input type="text" id="riskSearchInput" data-i18n-placeholder="searchRiskPlaceholder" placeholder="搜索风险网关..." oninput="riskTM.filter()"></div>
<div style="max-height:600px;overflow-y:auto;">
<table id="riskTable">
<thead><tr>
  <th>NO</th>
  <th onclick="riskTM.sort(0)">MAC <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(1)"><span data-i18n="thModel">型号</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(2)"><span data-i18n="thName">网关名称</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(3)"><span data-i18n="thVendor">厂家</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(4)">EST_TYP_A <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(5)">EST_TYP_B <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(6)"><span data-i18n="thEol">EOL_INFO</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(7)"><span data-i18n="thApp">应用版本</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(8)"><span data-i18n="thVersion">版本</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(9)"><span data-i18n="thStatus">状态</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(10)"><span data-i18n="thSysUptime">系统运行时长(s)</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(11)"><span data-i18n="thSysUptimeStr">运行时长</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(12)"><span data-i18n="thSectorsRead">读扇区数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(13)"><span data-i18n="thSectorsWritten">写扇区数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(14)"><span data-i18n="thBytesRead">读字节数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(15)"><span data-i18n="thBytesReadStr">读取量</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(16)"><span data-i18n="thBytesWritten">写字节数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(17)"><span data-i18n="thBytesWrittenStr">写入量</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(18)"><span data-i18n="thAvgReadRate">平均读速率(B/s)</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsRead × 512 ÷ Sys_Uptime&#10;单位: B/s&#10;含义: 自启动以来的累计平均读取速率" data-i18n-tip="tipAvgReadRate">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(19)"><span data-i18n="thAvgReadRateStr">平均读速率</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsRead × 512 ÷ Sys_Uptime&#10;含义: 自启动以来的累计平均读取速率&#10;可读格式自动换算 KB/s, MB/s" data-i18n-tip="tipAvgReadRateStr">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(20)"><span data-i18n="thAvgWriteRate">平均写速率(B/s)</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsWritten × 512 ÷ Sys_Uptime&#10;单位: B/s&#10;含义: 自启动以来的累计平均写入速率" data-i18n-tip="tipAvgWriteRate">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="riskTM.sort(21)"><span data-i18n="thAvgWriteRateStr">平均写速率</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsWritten × 512 ÷ Sys_Uptime&#10;含义: 自启动以来的累计平均写入速率&#10;可读格式自动换算 KB/s, MB/s" data-i18n-tip="tipAvgWriteRateStr">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
</tr></thead>
<tbody id="riskBody"></tbody>
</table>
</div>
</div>

<!-- 失败网关详情 -->
<div class="section-title"><span data-i18n="secFailed">失败网关详情</span> <span style="font-size:11px;color:#94a3b8;font-weight:400;">(<span data-i18n="failedCount">共 {failed_count} 台</span>)</span></div>
<div class="table-wrap">
<div class="search-bar"><input type="text" id="failedSearchInput" data-i18n-placeholder="searchFailedPlaceholder" placeholder="搜索失败网关..." oninput="failedTM.filter()"></div>
<div style="max-height:600px;overflow-y:auto;">
<table id="failedTable">
<thead><tr>
  <th>NO</th>
  <th onclick="failedTM.sort(0)">MAC <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(1)"><span data-i18n="thModel">型号</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(2)"><span data-i18n="thName">网关名称</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(3)"><span data-i18n="thApp">应用版本</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(4)"><span data-i18n="thVersion">版本</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(5)"><span data-i18n="thStatus">状态</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(6)"><span data-i18n="thSysUptime">系统运行时长(s)</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(7)"><span data-i18n="thSysUptimeStr">运行时长</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="failedTM.sort(8)"><span data-i18n="thError">错误信息</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
</tr></thead>
<tbody id="failedBody"></tbody>
</table>
</div>
</div>

<!-- 全量网关明细 -->
<div class="section-title" data-i18n="secDetail">全量网关明细</div>
<div class="table-wrap">
<div class="search-bar"><input type="text" id="detailSearchInput" data-i18n-placeholder="searchPlaceholder" placeholder="搜索网关名称、MAC、厂家..." oninput="detailTM.filter()"></div>
<div style="max-height:600px;overflow-y:auto;">
<table id="detailTable">
<thead><tr>
  <th>NO</th>
  <th onclick="detailTM.sort(0)">MAC <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(1)"><span data-i18n="thModel">型号</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(2)"><span data-i18n="thName">网关名称</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(3)">SN <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(4)"><span data-i18n="thVendor">厂家</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(5)" data-type="hex">EST_TYP_A <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(6)" data-type="hex">EST_TYP_B <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(7)" data-type="hex"><span data-i18n="thEol">EOL_INFO</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(8)"><span data-i18n="thApp">应用版本</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(9)"><span data-i18n="thVersion">版本</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(10)"><span data-i18n="thStatus">状态</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(11)"><span data-i18n="thUplink">连接方式</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(12)"><span data-i18n="thSysUptime">系统运行时长(s)</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(13)"><span data-i18n="thSysUptimeStr">运行时长</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(14)"><span data-i18n="thSectorsRead">读扇区数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(15)"><span data-i18n="thSectorsWritten">写扇区数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(16)"><span data-i18n="thBytesRead">读字节数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(17)"><span data-i18n="thBytesReadStr">读取量</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(18)"><span data-i18n="thBytesWritten">写字节数</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(19)"><span data-i18n="thBytesWrittenStr">写入量</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(20)"><span data-i18n="thAvgReadRate">平均读速率(B/s)</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsRead × 512 ÷ Sys_Uptime&#10;单位: B/s&#10;含义: 自启动以来的累计平均读取速率" data-i18n-tip="tipAvgReadRate">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(21)"><span data-i18n="thAvgReadRateStr">平均读速率</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsRead × 512 ÷ Sys_Uptime&#10;含义: 自启动以来的累计平均读取速率&#10;可读格式自动换算 KB/s, MB/s" data-i18n-tip="tipAvgReadRateStr">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(22)"><span data-i18n="thAvgWriteRate">平均写速率(B/s)</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsWritten × 512 ÷ Sys_Uptime&#10;单位: B/s&#10;含义: 自启动以来的累计平均写入速率" data-i18n-tip="tipAvgWriteRate">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
  <th onclick="detailTM.sort(23)"><span data-i18n="thAvgWriteRateStr">平均写速率</span><span class="th-tip" data-tip="数据来源: cat /proc/diskstats (mmcblk0p14)&#10;计算公式: SectorsWritten × 512 ÷ Sys_Uptime&#10;含义: 自启动以来的累计平均写入速率&#10;可读格式自动换算 KB/s, MB/s" data-i18n-tip="tipAvgWriteRateStr">?</span> <span class="sort-arrow">&#x25B4;&#x25BE;</span></th>
</tr></thead>
<tbody id="detailBody"></tbody>
</table>
</div>
</div>

</div><!-- /.container -->

<!-- 截图浮窗 -->
<div class="img-overlay" id="imgOverlay" onclick="closeScreenshot()"></div>
<div class="img-modal" id="imgModal">
  <div class="img-modal-header" id="imgModalHeader">
    <span id="imgModalTitle">Screenshot</span>
    <button class="img-modal-close" onclick="closeScreenshot()" title="Close">&times;</button>
  </div>
  <div class="img-modal-body" id="imgModalBody"></div>
  <div class="img-modal-actions">
    <button onclick="openScreenshotNewTab()" data-i18n="btnNewTab">新标签页打开</button>
  </div>
</div>

<script>
// ── i18n 字典 ─────────────────────────────────────────
const I18N = {{
  zh: {{
    title: 'eMMC 健康状态分析报告',
    metaGenTime: '生成时间', metaSource: '数据来源', metaTotal: '网关总数',
    metaOnline: '在线', metaVendors: 'eMMC 厂家',
    cardTotal: '网关总数', cardOnline: '在线网关', cardVendors: 'eMMC 厂家',
    cardRisk: '风险网关 (\\u22650x07)', cardFailed: '检测失败',
    secHealth: '健康等级概览 \\u2014 基于 EST_TYP_A 值分级，值越小越健康',
    secDist: 'EST_TYP_A 整体分布', secVendor: '按厂家 (devName) 分析',
    secRisk: '风险网关清单', secFailed: '失败网关详情', secDetail: '全量网关明细',
    riskCount: '共 {len(risk_devices)} 台', failedCount: '共 {failed_count} 台',
    lvHealthy: '健康', lvGood: '良好', lvWarning: '警告', lvDanger: '危险',
    chartDistTitle: '网关数量 vs EST_TYP_A 值',
    chartPieTitle: '各厂家网关占比', chartAvgTitle: '各厂家平均 EST_TYP_A',
    chartGroupTitle: '各厂家 EST_TYP_A 分布对比', chartStackTitle: '各厂家健康等级占比',
    axisDevCount: '网关数量', axisTypA: 'EST_TYP_A 值',
    axisAvgTypA: '平均 EST_TYP_A (十进制)', tipDevices: ' 台',
    labelDevCount: '网关数量', labelAvgTypA: '平均 EST_TYP_A',
    tipPieSuffix: ' 台',
    thModel: '型号', thName: '网关名称', thVendor: '厂家', thVersion: '版本',
    thStatus: '状态', thUplink: '连接方式', thApp: '应用版本', thEol: 'EOL_INFO',
    thSysUptime: '系统运行时长(s)', thSysUptimeStr: '运行时长', thError: '错误信息',
    thSectorsRead: '读扇区数', thSectorsWritten: '写扇区数',
    thBytesRead: '读字节数', thBytesReadStr: '读取量',
    thBytesWritten: '写字节数', thBytesWrittenStr: '写入量',
    thAvgReadRate: '平均读速率(B/s)', thAvgWriteRate: '平均写速率(B/s)',
    thAvgReadRateStr: '平均读速率', thAvgWriteRateStr: '平均写速率',
    tipAvgReadRate: '数据来源: cat /proc/diskstats (mmcblk0p14)\\n计算公式: SectorsRead × 512 ÷ Sys_Uptime\\n单位: B/s\\n含义: 自启动以来的累计平均读取速率',
    tipAvgReadRateStr: '数据来源: cat /proc/diskstats (mmcblk0p14)\\n计算公式: SectorsRead × 512 ÷ Sys_Uptime\\n含义: 自启动以来的累计平均读取速率\\n可读格式自动换算 KB/s, MB/s',
    tipAvgWriteRate: '数据来源: cat /proc/diskstats (mmcblk0p14)\\n计算公式: SectorsWritten × 512 ÷ Sys_Uptime\\n单位: B/s\\n含义: 自启动以来的累计平均写入速率',
    tipAvgWriteRateStr: '数据来源: cat /proc/diskstats (mmcblk0p14)\\n计算公式: SectorsWritten × 512 ÷ Sys_Uptime\\n含义: 自启动以来的累计平均写入速率\\n可读格式自动换算 KB/s, MB/s',
    searchPlaceholder: '搜索网关名称、MAC、型号、厂家...',
    searchRiskPlaceholder: '搜索风险网关...',
    searchFailedPlaceholder: '搜索失败网关...',
    noRisk: '无风险网关', noFailed: '无失败网关',
    toggleTheme: '切换主题', btnNewTab: '新标签页打开',
  }},
  en: {{
    title: 'eMMC Health Status Report',
    metaGenTime: 'Generated', metaSource: 'Data Source', metaTotal: 'Total Gateways',
    metaOnline: 'Online', metaVendors: 'eMMC Vendors',
    cardTotal: 'Total Gateways', cardOnline: 'Online Gateways', cardVendors: 'eMMC Vendors',
    cardRisk: 'Risk Gateways (\\u22650x07)', cardFailed: 'Failed',
    secHealth: 'Health Level Overview \\u2014 Based on EST_TYP_A value, lower is healthier',
    secDist: 'EST_TYP_A Overall Distribution', secVendor: 'Analysis by Vendor (devName)',
    secRisk: 'Risk Gateway List', secFailed: 'Failed Gateway Details', secDetail: 'All Gateway Details',
    riskCount: '{len(risk_devices)} total', failedCount: '{failed_count} total',
    lvHealthy: 'Healthy', lvGood: 'Good', lvWarning: 'Warning', lvDanger: 'Danger',
    chartDistTitle: 'Gateway Count vs EST_TYP_A',
    chartPieTitle: 'Gateway Share by Vendor', chartAvgTitle: 'Avg EST_TYP_A by Vendor',
    chartGroupTitle: 'EST_TYP_A Distribution by Vendor', chartStackTitle: 'Health Level by Vendor',
    axisDevCount: 'Gateway Count', axisTypA: 'EST_TYP_A Value',
    axisAvgTypA: 'Avg EST_TYP_A (decimal)', tipDevices: ' gateways',
    labelDevCount: 'Gateway Count', labelAvgTypA: 'Avg EST_TYP_A',
    tipPieSuffix: ' units',
    thModel: 'Model', thName: 'Gateway Name', thVendor: 'Vendor', thVersion: 'Version',
    thStatus: 'Status', thUplink: 'Uplink', thApp: 'App Version', thEol: 'EOL_INFO',
    thSysUptime: 'Sys Uptime(s)', thSysUptimeStr: 'Uptime', thError: 'Error',
    thSectorsRead: 'Sectors Read', thSectorsWritten: 'Sectors Written',
    thBytesRead: 'Bytes Read', thBytesReadStr: 'Read Volume',
    thBytesWritten: 'Bytes Written', thBytesWrittenStr: 'Write Volume',
    thAvgReadRate: 'Avg Read Rate(B/s)', thAvgWriteRate: 'Avg Write Rate(B/s)',
    thAvgReadRateStr: 'Avg Read Rate', thAvgWriteRateStr: 'Avg Write Rate',
    tipAvgReadRate: 'Source: cat /proc/diskstats (mmcblk0p14)\\nFormula: SectorsRead × 512 ÷ Sys_Uptime\\nUnit: B/s\\nMeaning: Cumulative avg read rate since boot',
    tipAvgReadRateStr: 'Source: cat /proc/diskstats (mmcblk0p14)\\nFormula: SectorsRead × 512 ÷ Sys_Uptime\\nMeaning: Cumulative avg read rate since boot\\nAuto-scaled to KB/s, MB/s',
    tipAvgWriteRate: 'Source: cat /proc/diskstats (mmcblk0p14)\\nFormula: SectorsWritten × 512 ÷ Sys_Uptime\\nUnit: B/s\\nMeaning: Cumulative avg write rate since boot',
    tipAvgWriteRateStr: 'Source: cat /proc/diskstats (mmcblk0p14)\\nFormula: SectorsWritten × 512 ÷ Sys_Uptime\\nMeaning: Cumulative avg write rate since boot\\nAuto-scaled to KB/s, MB/s',
    searchPlaceholder: 'Search name, MAC, model, vendor...',
    searchRiskPlaceholder: 'Search risk gateways...',
    searchFailedPlaceholder: 'Search failed gateways...',
    noRisk: 'No risk gateways', noFailed: 'No failed gateways',
    toggleTheme: 'Toggle theme', btnNewTab: 'Open in new tab',
  }},
  ja: {{
    title: 'eMMC 健康状態分析レポート',
    metaGenTime: '生成日時', metaSource: 'データソース', metaTotal: 'ゲートウェイ総数',
    metaOnline: 'オンライン', metaVendors: 'eMMC ベンダー',
    cardTotal: 'ゲートウェイ総数', cardOnline: 'オンラインGW', cardVendors: 'eMMC ベンダー',
    cardRisk: 'リスクGW (\\u22650x07)', cardFailed: '失敗GW',
    secHealth: '健康レベル概要 \\u2014 EST_TYP_A値に基づく分類、値が小さいほど健康',
    secDist: 'EST_TYP_A 全体分布', secVendor: 'ベンダー (devName) 別分析',
    secRisk: 'リスクGW一覧', secFailed: '失敗GW詳細', secDetail: '全GW明細',
    riskCount: '合計 {len(risk_devices)} 台', failedCount: '合計 {failed_count} 台',
    lvHealthy: '健康', lvGood: '良好', lvWarning: '警告', lvDanger: '危険',
    chartDistTitle: 'GW数 vs EST_TYP_A値',
    chartPieTitle: 'ベンダー別GW割合', chartAvgTitle: 'ベンダー別平均 EST_TYP_A',
    chartGroupTitle: 'ベンダー別 EST_TYP_A 分布比較', chartStackTitle: 'ベンダー別健康レベル割合',
    axisDevCount: 'GW数', axisTypA: 'EST_TYP_A値',
    axisAvgTypA: '平均 EST_TYP_A (10進)', tipDevices: ' GW',
    labelDevCount: 'GW数', labelAvgTypA: '平均 EST_TYP_A',
    tipPieSuffix: ' 台',
    thModel: 'モデル', thName: 'GW名', thVendor: 'ベンダー', thVersion: 'バージョン',
    thStatus: 'ステータス', thUplink: '接続方式', thApp: 'アプリバージョン', thEol: 'EOL_INFO',
    thSysUptime: 'システム稼働時間(s)', thSysUptimeStr: '稼働時間', thError: 'エラー',
    thSectorsRead: '読込セクタ数', thSectorsWritten: '書込セクタ数',
    thBytesRead: '読込バイト数', thBytesReadStr: '読取量',
    thBytesWritten: '書込バイト数', thBytesWrittenStr: '書込量',
    thAvgReadRate: '平均読込速度(B/s)', thAvgWriteRate: '平均書込速度(B/s)',
    thAvgReadRateStr: '平均読込速度', thAvgWriteRateStr: '平均書込速度',
    tipAvgReadRate: 'データソース: cat /proc/diskstats (mmcblk0p14)\\n計算式: SectorsRead × 512 ÷ Sys_Uptime\\n単位: B/s\\n意味: 起動以来の累計平均読込速度',
    tipAvgReadRateStr: 'データソース: cat /proc/diskstats (mmcblk0p14)\\n計算式: SectorsRead × 512 ÷ Sys_Uptime\\n意味: 起動以来の累計平均読込速度\\n可読形式 (KB/s, MB/s) 自動変換',
    tipAvgWriteRate: 'データソース: cat /proc/diskstats (mmcblk0p14)\\n計算式: SectorsWritten × 512 ÷ Sys_Uptime\\n単位: B/s\\n意味: 起動以来の累計平均書込速度',
    tipAvgWriteRateStr: 'データソース: cat /proc/diskstats (mmcblk0p14)\\n計算式: SectorsWritten × 512 ÷ Sys_Uptime\\n意味: 起動以来の累計平均書込速度\\n可読形式 (KB/s, MB/s) 自動変換',
    searchPlaceholder: 'GW名、MAC、モデル、ベンダーを検索...',
    searchRiskPlaceholder: 'リスクGWを検索...',
    searchFailedPlaceholder: '失敗GWを検索...',
    noRisk: 'リスクGWなし', noFailed: '失敗GWなし',
    toggleTheme: 'テーマ切替', btnNewTab: '新しいタブで開く',
  }},
}};
let curLang = 'zh';
function t(key) {{ return (I18N[curLang] && I18N[curLang][key]) || key; }}

// ── 语言切换 ──────────────────────────────────────────
function switchLang(lang) {{
  curLang = lang;
  const langMap = {{ zh: 'zh-CN', en: 'en', ja: 'ja' }};
  document.documentElement.lang = langMap[lang] || lang;
  document.querySelectorAll('.lang-btn').forEach(b => {{
    b.classList.toggle('active', b.textContent.trim() === (lang === 'zh' ? '中' : lang === 'en' ? 'EN' : 'JP'));
  }});
  document.querySelectorAll('[data-i18n]').forEach(el => {{
    const key = el.getAttribute('data-i18n');
    const val = t(key);
    if (val !== key) el.innerHTML = val;
  }});
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {{
    const key = el.getAttribute('data-i18n-placeholder');
    el.placeholder = t(key);
  }});
  document.querySelectorAll('[data-i18n-tip]').forEach(el => {{
    const key = el.getAttribute('data-i18n-tip');
    const val = t(key);
    if (val !== key) el.setAttribute('data-tip', val);
  }});
  document.querySelector('.theme-btn').title = t('toggleTheme');
  document.title = t('title');
  LEVELS[0].name = t('lvHealthy');
  LEVELS[1].name = t('lvGood');
  LEVELS[2].name = t('lvWarning');
  LEVELS[3].name = t('lvDanger');
  updateChartLang();
  riskTM.render(); failedTM.render(); detailTM.render();
}}

function updateChartLang() {{
  const ci = Chart.instances;
  Object.values(ci).forEach(c => {{
    const cid = c.canvas.id;
    if (cid === 'chartDist') {{
      c.data.datasets[0].label = t('labelDevCount');
      c.options.scales.y.title.text = t('axisDevCount');
      c.options.scales.x.title.text = t('axisTypA');
      c.options.plugins.tooltip.callbacks.label = ctx => ctx.parsed.y + t('tipDevices');
    }} else if (cid === 'chartAvg') {{
      c.data.datasets[0].label = t('labelAvgTypA');
      c.options.scales.x.title.text = t('axisAvgTypA');
    }} else if (cid === 'chartGrouped') {{
      c.options.scales.y.title.text = t('axisDevCount');
      c.options.scales.x.title.text = t('axisTypA');
    }} else if (cid === 'chartStacked') {{
      c.options.scales.y.title.text = t('axisDevCount');
      c.data.datasets[0].label = t('lvHealthy');
      c.data.datasets[1].label = t('lvGood');
      c.data.datasets[2].label = t('lvWarning');
      c.data.datasets[3].label = t('lvDanger');
    }} else if (cid === 'chartPie') {{
      c.options.plugins.tooltip.callbacks.label = ctx =>
        ctx.label + ': ' + ctx.parsed + t('tipPieSuffix') + ' (' + (ctx.parsed/{max(valid_count, 1)}*100).toFixed(1) + '%)';
    }}
    c.update('none');
  }});
}}

// ── 主题切换 ──────────────────────────────────────────
function toggleTheme() {{
  const r = document.documentElement;
  r.classList.toggle('light');
  const btn = document.querySelector('.theme-btn');
  btn.innerHTML = r.classList.contains('light') ? '&#9728;' : '&#9790;';
  applyChartTheme();
}}

function applyChartTheme() {{
  const isLight = document.documentElement.classList.contains('light');
  const txtColor = isLight ? '#64748b' : '#94a3b8';
  const gridColor = isLight ? 'rgba(0,0,0,0.08)' : 'rgba(255,255,255,0.06)';
  Chart.defaults.color = txtColor;
  Chart.defaults.borderColor = gridColor;
  Object.values(Chart.instances).forEach(c => {{
    if (c.options.scales) {{
      Object.values(c.options.scales).forEach(s => {{
        if (s.ticks) s.ticks.color = txtColor;
        if (s.title) s.title.color = txtColor;
        s.grid = s.grid || {{}};
        s.grid.color = gridColor;
      }});
    }}
    if (c.options.plugins && c.options.plugins.legend) {{
      c.options.plugins.legend.labels = c.options.plugins.legend.labels || {{}};
      c.options.plugins.legend.labels.color = txtColor;
    }}
    c.data.datasets.forEach(ds => {{
      if (ds.borderColor === '#0f172a' || ds.borderColor === '#f8fafc') {{
        ds.borderColor = isLight ? '#f8fafc' : '#0f172a';
      }}
    }});
    c.update('none');
  }});
}}

// ── 嵌入原始数据 ──────────────────────────────────────
const RAW_DATA = {data_json};
const RISK_DATA = {risk_json};
const FAILED_DATA = {failed_json};

// ── 健康等级映射 ──────────────────────────────────────
const LEVELS = [
    {{name:'健康', min:1, max:3, color:'#22c55e', cls:'health-badge-good'}},
    {{name:'良好', min:4, max:6, color:'#f59e0b', cls:'health-badge-warn'}},
    {{name:'警告', min:7, max:9, color:'#f97316', cls:'health-badge-alert'}},
    {{name:'危险', min:10, max:11, color:'#ef4444', cls:'health-badge-bad'}},
];
function getLevel(v) {{
    for (const lv of LEVELS) if (v >= lv.min && v <= lv.max) return lv;
    return LEVELS[LEVELS.length-1];
}}
function hexToInt(s) {{
    const n = parseInt(s, 16);
    return isNaN(n) ? -1 : n;
}}
function esc(s) {{
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(s));
    return d.innerHTML;
}}
function macCell(mac) {{
    if (!mac) return '';
    const macFile = mac.replace(/:/g, '-');
    return '<span class="mac-link" onclick="showScreenshot(\\x27' + esc(macFile) + '\\x27,\\x27' + esc(mac) + '\\x27,this)">' + esc(mac) + '</span>';
}}

// ── Chart.js 全局配置 ─────────────────────────────────
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = 'rgba(255,255,255,0.06)';
Chart.defaults.font.family = '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif';
Chart.defaults.font.size = 11;

// ── 图表 ─────────────────────────────────────────────
new Chart(document.getElementById('chartDist'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(dist_labels)},
        datasets: [{{
            label: '网关数量',
            data: {json.dumps(dist_values)},
            backgroundColor: {json.dumps(dist_colors)},
            borderRadius: 4,
            maxBarThickness: 60,
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{
            legend: {{ display: false }},
            tooltip: {{ callbacks: {{ label: ctx => ctx.parsed.y + t('tipDevices') }} }}
        }},
        scales: {{
            y: {{ beginAtZero: true, title: {{ display: true, text: '网关数量' }}, ticks: {{ stepSize: 1 }} }},
            x: {{ title: {{ display: true, text: 'EST_TYP_A 值' }} }}
        }}
    }}
}});

new Chart(document.getElementById('chartPie'), {{
    type: 'doughnut',
    data: {{
        labels: {pie_labels},
        datasets: [{{ data: {pie_values}, backgroundColor: {pie_colors}, borderWidth: 2, borderColor: '#0f172a' }}]
    }},
    options: {{
        responsive: true,
        plugins: {{
            legend: {{ position: 'bottom' }},
            tooltip: {{ callbacks: {{ label: ctx => ctx.label + ': ' + ctx.parsed + t('tipPieSuffix') + ' (' + (ctx.parsed/{max(valid_count, 1)}*100).toFixed(1) + '%)' }} }}
        }}
    }}
}});

new Chart(document.getElementById('chartAvg'), {{
    type: 'bar',
    data: {{
        labels: {avg_labels},
        datasets: [{{
            label: '平均 EST_TYP_A',
            data: {avg_values},
            backgroundColor: {avg_colors},
            borderRadius: 4,
            maxBarThickness: 60,
        }}]
    }},
    options: {{
        indexAxis: 'y',
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
            x: {{ beginAtZero: true, title: {{ display: true, text: '平均 EST_TYP_A (十进制)' }} }}
        }}
    }}
}});

new Chart(document.getElementById('chartGrouped'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(dist_labels)},
        datasets: [{grouped_datasets_js}]
    }},
    options: {{
        responsive: true,
        plugins: {{
            legend: {{ position: 'top' }},
            tooltip: {{
                filter: ctx => ctx.parsed.y !== null && ctx.parsed.y > 0,
                callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y + t('tipDevices') }}
            }}
        }},
        scales: {{
            y: {{ beginAtZero: true, title: {{ display: true, text: '网关数量' }}, ticks: {{ stepSize: 1 }} }},
            x: {{
                title: {{ display: true, text: 'EST_TYP_A 值' }},
                grid: {{ display: false }}
            }}
        }}
    }},
    plugins: [{{
        id: 'groupedBarLabels',
        afterDatasetsDraw(chart) {{
            const ctx = chart.ctx;
            chart.data.datasets.forEach((ds, i) => {{
                const meta = chart.getDatasetMeta(i);
                meta.data.forEach((bar, idx) => {{
                    const val = ds.data[idx];
                    if (val === null || val === 0) return;
                    ctx.save();
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'bottom';
                    ctx.font = 'bold 10px -apple-system,BlinkMacSystemFont,sans-serif';
                    ctx.fillStyle = ds.backgroundColor;
                    ctx.fillText(val, bar.x, bar.y - 2);
                    ctx.restore();
                }});
            }});
        }}
    }}]
}});

new Chart(document.getElementById('chartStacked'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(dev_names)},
        datasets: [{stacked_datasets_js}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ position: 'top' }} }},
        scales: {{
            x: {{ stacked: true }},
            y: {{ stacked: true, beginAtZero: true, title: {{ display: true, text: '网关数量' }} }}
        }}
    }}
}});

// ── 通用表格管理器工厂 ────────────────────────────────
function createTableManager(rawData, tbodyId, searchInputId, columns, opts) {{
    opts = opts || {{}};
    let dataset = rawData;
    let sortCol = -1, sortAsc = true;

    function render(ds) {{
        if (ds) dataset = ds;
        const tbody = document.getElementById(tbodyId);
        let html = '';
        for (let i = 0; i < dataset.length; i++) {{
            const d = dataset[i];
            html += '<tr><td>' + (i + 1) + '</td>';
            for (const col of columns) {{
                html += col.render(d);
            }}
            html += '</tr>';
        }}
        tbody.innerHTML = html || '<tr><td colspan="' + (columns.length + 1) + '" style="text-align:center;color:var(--text-dim);padding:24px;">' + (opts.emptyText || '') + '</td></tr>';
    }}

    function filter() {{
        const q = document.getElementById(searchInputId).value.toLowerCase();
        const searchFields = opts.searchFields || [];
        dataset = rawData.filter(d => {{
            for (const f of searchFields) {{
                if ((d[f] || '').toString().toLowerCase().includes(q)) return true;
            }}
            return false;
        }});
        render();
    }}

    function sort(colIdx) {{
        if (sortCol === colIdx) {{ sortAsc = !sortAsc; }} else {{ sortCol = colIdx; sortAsc = true; }}
        const col = columns[colIdx];
        const key = col.key;
        const isHex = col.hex || false;
        const isNum = col.num || false;
        dataset = [...dataset].sort((a, b) => {{
            let va = a[key] || '', vb = b[key] || '';
            if (isHex) {{ va = hexToInt(va); vb = hexToInt(vb); return sortAsc ? va - vb : vb - va; }}
            if (isNum) {{ va = Number(va) || 0; vb = Number(vb) || 0; return sortAsc ? va - vb : vb - va; }}
            return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
        }});
        render();
    }}

    render();
    return {{ render, filter, sort }};
}}

// ── 风险表 ────────────────────────────────────────────
const riskTM = createTableManager(RISK_DATA, 'riskBody', 'riskSearchInput', [
    {{ key:'mac', render: d => '<td>' + macCell(d.mac||'') + '</td>' }},
    {{ key:'model', render: d => '<td>' + esc(d.model||'') + '</td>' }},
    {{ key:'name', render: d => '<td>' + esc(d.name||'') + '</td>' }},
    {{ key:'devName', render: d => '<td>' + esc(d.devName||'') + '</td>' }},
    {{ key:'EST_TYP_A', hex:true, render: d => {{
        const v = hexToInt(d.EST_TYP_A||'0x00'); const lv = getLevel(v);
        return '<td><span class="badge '+lv.cls+'">' + esc(d.EST_TYP_A||'') + ' (' + v + ')</span></td>';
    }} }},
    {{ key:'EST_TYP_B', hex:true, render: d => '<td>' + esc(d.EST_TYP_B||'') + '</td>' }},
    {{ key:'EOL_INFO', hex:true, render: d => '<td>' + esc(d.EOL_INFO||'') + '</td>' }},
    {{ key:'appVersion', render: d => '<td>' + esc(d.appVersion||'') + '</td>' }},
    {{ key:'version', render: d => '<td>' + esc(d.version||'') + '</td>' }},
    {{ key:'status', render: d => '<td>' + esc(d.status||'') + '</td>' }},
    {{ key:'sys_uptime', num:true, render: d => '<td>' + esc(String(d.sys_uptime||'')) + '</td>' }},
    {{ key:'sys_uptime_str', render: d => '<td>' + esc(d.sys_uptime_str||'') + '</td>' }},
    {{ key:'ds_sectors_read', num:true, render: d => '<td>' + esc(String(d.ds_sectors_read||'')) + '</td>' }},
    {{ key:'ds_sectors_written', num:true, render: d => '<td>' + esc(String(d.ds_sectors_written||'')) + '</td>' }},
    {{ key:'ds_bytes_read', num:true, render: d => '<td>' + (d.ds_bytes_read !== '' && d.ds_bytes_read != null ? Number(d.ds_bytes_read) : '') + '</td>' }},
    {{ key:'ds_bytes_read_str', render: d => '<td>' + esc(d.ds_bytes_read_str||'') + '</td>' }},
    {{ key:'ds_bytes_written', num:true, render: d => '<td>' + (d.ds_bytes_written !== '' && d.ds_bytes_written != null ? Number(d.ds_bytes_written) : '') + '</td>' }},
    {{ key:'ds_bytes_written_str', render: d => '<td>' + esc(d.ds_bytes_written_str||'') + '</td>' }},
    {{ key:'ds_avg_read_rate', num:true, render: d => '<td>' + (d.ds_avg_read_rate !== '' && d.ds_avg_read_rate != null ? Number(d.ds_avg_read_rate).toFixed(2) : '') + '</td>' }},
    {{ key:'ds_avg_read_rate_str', render: d => '<td>' + esc(d.ds_avg_read_rate_str||'') + '</td>' }},
    {{ key:'ds_avg_write_rate', num:true, render: d => '<td>' + (d.ds_avg_write_rate !== '' && d.ds_avg_write_rate != null ? Number(d.ds_avg_write_rate).toFixed(2) : '') + '</td>' }},
    {{ key:'ds_avg_write_rate_str', render: d => '<td>' + esc(d.ds_avg_write_rate_str||'') + '</td>' }},
], {{
    searchFields: ['mac','model','name','devName','appVersion','EST_TYP_A','EOL_INFO','ds_bytes_read_str','ds_bytes_written_str','ds_avg_read_rate_str','ds_avg_write_rate_str'],
    emptyText: t('noRisk'),
}});

// ── 失败表 ────────────────────────────────────────────
const failedTM = createTableManager(FAILED_DATA, 'failedBody', 'failedSearchInput', [
    {{ key:'mac', render: d => '<td>' + macCell(d.mac||'') + '</td>' }},
    {{ key:'model', render: d => '<td>' + esc(d.model||'') + '</td>' }},
    {{ key:'name', render: d => '<td>' + esc(d.name||'') + '</td>' }},
    {{ key:'appVersion', render: d => '<td>' + esc(d.appVersion||'') + '</td>' }},
    {{ key:'version', render: d => '<td>' + esc(d.version||'') + '</td>' }},
    {{ key:'status', render: d => '<td>' + esc(d.status||'') + '</td>' }},
    {{ key:'sys_uptime', num:true, render: d => '<td>' + esc(String(d.sys_uptime||'')) + '</td>' }},
    {{ key:'sys_uptime_str', render: d => '<td>' + esc(d.sys_uptime_str||'') + '</td>' }},
    {{ key:'_error', render: d => '<td style="white-space:normal;max-width:300px;">' + esc(d._error||'') + '</td>' }},
], {{
    searchFields: ['mac','model','name','appVersion','_error'],
    emptyText: t('noFailed'),
}});

// ── 全量明细表 ────────────────────────────────────────
const detailTM = createTableManager(RAW_DATA, 'detailBody', 'detailSearchInput', [
    {{ key:'mac', render: d => '<td>' + macCell(d.mac||'') + '</td>' }},
    {{ key:'model', render: d => '<td>' + esc(d.model||'') + '</td>' }},
    {{ key:'name', render: d => '<td>' + esc(d.name||'') + '</td>' }},
    {{ key:'sn', render: d => '<td>' + esc(d.sn||'') + '</td>' }},
    {{ key:'devName', render: d => '<td>' + esc(d.devName||'') + '</td>' }},
    {{ key:'EST_TYP_A', hex:true, render: d => {{
        const v = hexToInt(d.EST_TYP_A||'0x00'); const lv = getLevel(v);
        return '<td><span class="badge '+lv.cls+'">' + esc(d.EST_TYP_A||'') + ' (' + v + ')</span></td>';
    }} }},
    {{ key:'EST_TYP_B', hex:true, render: d => '<td>' + esc(d.EST_TYP_B||'') + '</td>' }},
    {{ key:'EOL_INFO', hex:true, render: d => '<td>' + esc(d.EOL_INFO||'') + '</td>' }},
    {{ key:'appVersion', render: d => '<td>' + esc(d.appVersion||'') + '</td>' }},
    {{ key:'version', render: d => '<td>' + esc(d.version||'') + '</td>' }},
    {{ key:'status', render: d => '<td>' + esc(d.status||'') + '</td>' }},
    {{ key:'uplink', render: d => '<td>' + esc(d.uplink||'') + '</td>' }},
    {{ key:'sys_uptime', num:true, render: d => '<td>' + esc(String(d.sys_uptime||'')) + '</td>' }},
    {{ key:'sys_uptime_str', render: d => '<td>' + esc(d.sys_uptime_str||'') + '</td>' }},
    {{ key:'ds_sectors_read', num:true, render: d => '<td>' + esc(String(d.ds_sectors_read||'')) + '</td>' }},
    {{ key:'ds_sectors_written', num:true, render: d => '<td>' + esc(String(d.ds_sectors_written||'')) + '</td>' }},
    {{ key:'ds_bytes_read', num:true, render: d => '<td>' + (d.ds_bytes_read !== '' && d.ds_bytes_read != null ? Number(d.ds_bytes_read) : '') + '</td>' }},
    {{ key:'ds_bytes_read_str', render: d => '<td>' + esc(d.ds_bytes_read_str||'') + '</td>' }},
    {{ key:'ds_bytes_written', num:true, render: d => '<td>' + (d.ds_bytes_written !== '' && d.ds_bytes_written != null ? Number(d.ds_bytes_written) : '') + '</td>' }},
    {{ key:'ds_bytes_written_str', render: d => '<td>' + esc(d.ds_bytes_written_str||'') + '</td>' }},
    {{ key:'ds_avg_read_rate', num:true, render: d => '<td>' + (d.ds_avg_read_rate !== '' && d.ds_avg_read_rate != null ? Number(d.ds_avg_read_rate).toFixed(2) : '') + '</td>' }},
    {{ key:'ds_avg_read_rate_str', render: d => '<td>' + esc(d.ds_avg_read_rate_str||'') + '</td>' }},
    {{ key:'ds_avg_write_rate', num:true, render: d => '<td>' + (d.ds_avg_write_rate !== '' && d.ds_avg_write_rate != null ? Number(d.ds_avg_write_rate).toFixed(2) : '') + '</td>' }},
    {{ key:'ds_avg_write_rate_str', render: d => '<td>' + esc(d.ds_avg_write_rate_str||'') + '</td>' }},
], {{
    searchFields: ['mac','model','name','sn','devName','appVersion','EOL_INFO','ds_bytes_read_str','ds_bytes_written_str','ds_avg_read_rate_str','ds_avg_write_rate_str'],
    emptyText: '',
}});

// ── 截图浮窗 ──────────────────────────────────────────
let _screenshotSrc = '';
let _highlightedRow = null;
function _clearHighlight() {{
    if (_highlightedRow) {{ _highlightedRow.classList.remove('row-highlight'); _highlightedRow = null; }}
}}
function showScreenshot(macFile, macDisplay, el) {{
    _screenshotSrc = 'screenshots/' + macFile + '.png';
    _clearHighlight();
    if (el) {{
        const tr = el.closest('tr');
        if (tr) {{ tr.classList.add('row-highlight'); _highlightedRow = tr; }}
    }}
    const modal = document.getElementById('imgModal');
    const overlay = document.getElementById('imgOverlay');
    const body = document.getElementById('imgModalBody');
    const title = document.getElementById('imgModalTitle');
    title.textContent = macDisplay;
    modal.style.width = '';
    body.innerHTML = '<div class="img-error" data-i18n="imgLoading">加载中...</div>';
    modal.classList.add('show');
    overlay.classList.add('show');
    const mw = Math.min(1100, window.innerWidth * 0.96);
    modal.style.left = Math.max(0, (window.innerWidth - mw) / 2) + 'px';
    modal.style.top = '20px';
    const img = new Image();
    img.onload = function() {{
        body.innerHTML = '';
        body.appendChild(img);
    }};
    img.onerror = function() {{
        body.innerHTML = '<div class="img-error">' + esc(macDisplay) + '.png not found</div>';
    }};
    img.src = _screenshotSrc;
    img.style.maxWidth = '100%';
    img.style.borderRadius = '6px';
}}
function closeScreenshot() {{
    document.getElementById('imgModal').classList.remove('show');
    document.getElementById('imgOverlay').classList.remove('show');
    _clearHighlight();
}}
function openScreenshotNewTab() {{
    if (_screenshotSrc) window.open(_screenshotSrc, '_blank');
}}
document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') closeScreenshot();
}});

// ── 拖拽逻辑 ──────────────────────────────────────────
(function() {{
    const header = document.getElementById('imgModalHeader');
    const modal = document.getElementById('imgModal');
    let isDragging = false, startX, startY, origX, origY;
    header.addEventListener('mousedown', function(e) {{
        if (e.target.closest('.img-modal-close')) return;
        isDragging = true;
        startX = e.clientX; startY = e.clientY;
        const rect = modal.getBoundingClientRect();
        origX = rect.left; origY = rect.top;
        modal.style.width = rect.width + 'px';
        e.preventDefault();
    }});
    document.addEventListener('mousemove', function(e) {{
        if (!isDragging) return;
        modal.style.left = (origX + e.clientX - startX) + 'px';
        modal.style.top = (origY + e.clientY - startY) + 'px';
    }});
    document.addEventListener('mouseup', function() {{ isDragging = false; }});
    header.addEventListener('touchstart', function(e) {{
        if (e.target.closest('.img-modal-close')) return;
        const touch = e.touches[0];
        isDragging = true;
        startX = touch.clientX; startY = touch.clientY;
        const rect = modal.getBoundingClientRect();
        origX = rect.left; origY = rect.top;
        modal.style.width = rect.width + 'px';
    }}, {{passive: true}});
    document.addEventListener('touchmove', function(e) {{
        if (!isDragging) return;
        const touch = e.touches[0];
        modal.style.left = (origX + touch.clientX - startX) + 'px';
        modal.style.top = (origY + touch.clientY - startY) + 'px';
    }});
    document.addEventListener('touchend', function() {{ isDragging = false; }});
}})();
</script>
</body>
</html>"""

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"报告已生成: {OUTPUT_FILE}")
    print(f"总网关: {total}, 在线: {online_count}, 厂家: {vendor_count}, 风险: {len(risk_devices)}, 失败: {failed_count}")


if __name__ == "__main__":
    generate()
