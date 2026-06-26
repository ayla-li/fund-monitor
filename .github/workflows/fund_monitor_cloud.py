#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基金智能监控 - GitHub Actions 云端版 v5.0
功能：
  ✓ 双账户统一监控（支付宝 + A平台）
  ✓ 实时估值获取与收益计算
  ✓ matplotlib PNG 图表生成（供Bark图片推送）
  ✓ HTML 看板生成（供点击查看详情）
  ✓ 智能建议 + 预警
  ✓ 早盘收益预测
"""

import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings('ignore')
os.environ['MPLBACKEND'] = 'Agg'

import requests
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# ============== 路径配置 ==============
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / 'fund_config.json'
CHARTS_DIR = BASE_DIR / 'charts'
SUMMARIES_DIR = BASE_DIR / 'summaries'
CHARTS_DIR.mkdir(exist_ok=True)
SUMMARIES_DIR.mkdir(exist_ok=True)

# ============== 加载配置 ==============
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    CONFIG = json.load(f)

HOLDINGS = CONFIG.get('holdings', [])
BARK_KEY = os.environ.get('BARK_KEY', CONFIG.get('bark_key', ''))

# 预警规则
ALERT_RULES = {
    'single_fund_drop_pct': -3,
    'single_fund_rise_pct': 5,
    'stop_loss_pct': -8,
    'take_profit_pct': 20,
}

# ============== 数据获取 ==============
def get_fund_realtime(fund_code):
    try:
        code = str(fund_code).strip().zfill(6)
        url = f'http://fundgz.1234567.com.cn/js/{code}.js'
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        if 'jsonpgz(' not in r.text:
            return None
        json_str = r.text[r.text.find('(')+1:r.text.rfind(')')]
        d = json.loads(json_str)
        return {
            'code': d.get('fundcode'),
            'name': d.get('name'),
            'nav': float(d.get('dwjz', 0)),
            'estimate_nav': float(d.get('gsz', 0)),
            'estimate_rate': float(d.get('gszzl', 0)),
            'nav_date': d.get('jzrq'),
            'estimate_time': d.get('gztime'),
        }
    except Exception:
        return None


def get_index_realtime():
    indices = {}
    index_map = {
        '000001': ('1.000001', '上证指数'),
        '000300': ('1.000300', '沪深300'),
        '399006': ('0.399006', '创业板指'),
        '000688': ('1.000688', '科创50'),
    }
    for code, (secid, name) in index_map.items():
        try:
            url = f'https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f44,f45,f46,f58,f60'
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
            d = r.json().get('data') or {}
            price = d.get('f43', 0) / 100
            prev = d.get('f60', 0) / 100 if d.get('f60') else price
            indices[code] = {
                'name': name, 'price': price, 'prev': prev,
                'change_pct': round((price-prev)/prev*100, 2) if prev else 0
            }
        except Exception:
            pass
    return indices


# ============== 收益计算 ==============
def calculate_all_holdings():
    results = []
    total_cost = 0
    total_market = 0
    total_today_profit = 0

    for h in HOLDINGS:
        rt = get_fund_realtime(h['code'])
        if not rt:
            results.append({**h, 'status': 'error', 'message': '数据获取失败'})
            continue

        if 'shares' in h and h['shares'] > 0:
            shares = h['shares']
            cost_price = h.get('cost_price', rt['nav'])
            cost_value = cost_price * shares
            market_value = rt['estimate_nav'] * shares
        elif 'cost_amount' in h and h['cost_amount'] > 0:
            cost_value = h['cost_amount']
            shares = cost_value / rt['nav'] if rt['nav'] > 0 else 0
            market_value = rt['estimate_nav'] * shares
        else:
            continue

        profit = market_value - cost_value
        profit_pct = (profit / cost_value * 100) if cost_value > 0 else 0
        today_profit = market_value * rt['estimate_rate'] / 100

        total_cost += cost_value
        total_market += market_value
        total_today_profit += today_profit

        # 建议
        advice = []
        if rt['estimate_rate'] <= -3:
            advice.append(f'今日大跌 {rt["estimate_rate"]:+.2f}%，关注加仓机会')
        elif rt['estimate_rate'] >= 3:
            advice.append(f'今日大涨 {rt["estimate_rate"]:+.2f}%，持有观望')
        if profit_pct <= -8:
            advice.append(f'累计亏损 {profit_pct:.1f}%，触发止损线')
        elif profit_pct >= 20:
            advice.append(f'累计盈利 {profit_pct:.1f}%，可考虑止盈')

        results.append({
            **h, 'status': 'ok',
            'nav': rt['nav'], 'estimate_nav': rt['estimate_nav'],
            'estimate_rate': rt['estimate_rate'],
            'cost_value': round(cost_value, 2),
            'market_value': round(market_value, 2),
            'profit': round(profit, 2),
            'profit_pct': round(profit_pct, 2),
            'today_profit': round(today_profit, 2),
            'advice': advice,
        })

    return {
        'funds': results,
        'total_cost': round(total_cost, 2),
        'total_market': round(total_market, 2),
        'total_profit': round(total_market - total_cost, 2),
        'total_profit_pct': round((total_market - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
        'total_today_profit': round(total_today_profit, 2),
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# ============== 收益预测 ==============
def predict_today_profit(status, indices):
    funds = [f for f in status['funds'] if f['status'] == 'ok']
    if not funds:
        return None
    a_share_value = sum(f['market_value'] for f in funds if 'QDII' not in f.get('region', 'A股'))
    qdii_value = sum(f['market_value'] for f in funds if 'QDII' in f.get('region', ''))
    bond_value = sum(f['market_value'] for f in funds if '债' in f.get('type', ''))
    equity_value = sum(f['market_value'] for f in funds if '债' not in f.get('type', '') and '货币' not in f.get('type', ''))

    hs300 = indices.get('000300', {}).get('change_pct', 0)
    cy = indices.get('399006', {}).get('change_pct', 0)
    avg = hs300 * 0.6 + cy * 0.4
    beta = 1.2
    pred = a_share_value * avg / 100 * beta
    vol = abs(avg) * 0.5 + 0.5
    return {
        'predict_low': round(pred - equity_value * vol / 100, 0),
        'predict_high': round(pred + equity_value * vol / 100, 0),
        'predict_mid': round(pred, 0),
        'direction': '上涨' if pred > 0 else '下跌' if pred < 0 else '震荡',
        'basis': f'沪深300 {hs300:+.2f}% / 创业板 {cy:+.2f}%',
    }


# ============== 图表生成（PNG）==============
def generate_chart_png(status, mode='close'):
    """生成简洁PNG图表，供Bark通知显示"""
    funds = [f for f in status['funds'] if f['status'] == 'ok']
    if not funds:
        return None
    now = datetime.now()
    mode_names = {'morning': '上午预测', 'midday': '盘中监控', 'close': '收盘总结'}

    # 设置中文字体
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'WenQuanYi Micro Hei', 'SimHei', 'Arial Unicode MS']
    plt.rcParams['axes.unicode_minus'] = False

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5), facecolor='#1a1a2e')
    fig.patch.set_facecolor('#1a1a2e')

    # 左图：今日盈亏大数字 + 分布
    ax1 = axes[0]
    ax1.set_facecolor('#1a1a2e')
    ax1.axis('off')

    total = status['total_today_profit']
    color = '#00e676' if total >= 0 else '#ff5252'
    arrow = '▲' if total >= 0 else '▼'

    ax1.text(0.5, 0.85, mode_names.get(mode, '监控'), fontsize=14, color='#8899aa',
             ha='center', transform=ax1.transAxes, fontweight='bold')
    ax1.text(0.5, 0.55, f'{arrow} ¥{total:+,.0f}', fontsize=36, color=color,
             ha='center', transform=ax1.transAxes, fontweight='bold')
    ax1.text(0.5, 0.30, '今日盈亏', fontsize=11, color='#667788',
             ha='center', transform=ax1.transAxes)
    ax1.text(0.5, 0.12, f'累计盈亏 ¥{status["total_profit"]:+,.0f} ({status["total_profit_pct"]:+.1f}%)',
             fontsize=9, color='#556677', ha='center', transform=ax1.transAxes)

    # 右图：TOP10 基金今日盈亏柱状图
    ax2 = axes[1]
    ax2.set_facecolor('#1a1a2e')
    top10 = sorted(funds, key=lambda x: -abs(x['today_profit']))[:10]
    names = [f['name'][:6] for f in top10]
    vals = [f['today_profit'] for f in top10]
    colors = ['#00e676' if v >= 0 else '#ff5252' for v in vals]

    bars = ax2.barh(range(len(names)), vals, color=colors, height=0.6, alpha=0.85)
    ax2.set_yticks(range(len(names)))
    ax2.set_yticklabels(names, color='#aabbcc', fontsize=8)
    ax2.tick_params(axis='x', colors='#667788', labelsize=8)
    ax2.invert_yaxis()
    ax2.axvline(x=0, color='#445566', linewidth=0.5)
    ax2.set_xlabel('今日盈亏 (元)', color='#667788', fontsize=9)
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    ax2.spines['bottom'].set_color('#334455')
    ax2.spines['left'].set_color('#334455')

    # 在柱子上标注数值
    for bar, val in zip(bars, vals):
        x = bar.get_width()
        offset = 3 if x >= 0 else -3
        align = 'left' if x >= 0 else 'right'
        ax2.annotate(f'¥{val:+.0f}', xy=(x, bar.get_y() + bar.get_height()/2),
                    xytext=(offset, 0), textcoords='offset points',
                    ha=align, va='center', fontsize=7, color='#ccddee')

    plt.tight_layout(pad=2)
    filename = f'chart_{mode}_{now.strftime("%Y%m%d_%H%M")}.png'
    filepath = CHARTS_DIR / filename
    plt.savefig(filepath, dpi=120, facecolor='#1a1a2e', edgecolor='none',
                bbox_inches='tight', pad_inches=0.15)
    plt.close()
    return filename


# ============== HTML看板生成 ==============
def generate_summary_html(status, indices, mode='close'):
    """生成详细HTML看板页面"""
    funds = [f for f in status['funds'] if f['status'] == 'ok']
    now = datetime.now()
    mode_names = {'morning': '上午预测', 'midday': '盘中监控', 'close': '收盘总结'}

    total = status['total_today_profit']
    color = '#00e676' if total >= 0 else '#ff5252'
    arrow = '▲' if total >= 0 else '▼'
    up = sum(1 for f in funds if f['estimate_rate'] > 0)
    down = sum(1 for f in funds if f['estimate_rate'] < 0)

    # 预警基金
    alerts = [f for f in funds if f['estimate_rate'] <= -3 or f['estimate_rate'] >= 5 or f['profit_pct'] <= -8]

    # 预测（仅上午模式）
    prediction = predict_today_profit(status, indices) if mode == 'morning' else None

    # 基金明细行
    rows = []
    sorted_funds = sorted(funds, key=lambda x: -abs(x['today_profit']))
    for f in sorted_funds:
        c = '#00e676' if f['today_profit'] >= 0 else '#ff5252'
        pc = '#00e676' if f['profit'] >= 0 else '#ff5252'
        advice_tags = ' '.join([f'<span class="tag">{a}</span>' for a in f['advice']])
        rows.append(f'''<tr>
<td><div class="fname">{f['name']}</div><div class="fcode">{f['code']} · {f['platform']}</div></td>
<td class="num" style="color:{c}">{f['estimate_rate']:+.2f}%</td>
<td class="num" style="color:{c}">¥{f['today_profit']:+,.0f}</td>
<td class="num" style="color:{pc}">¥{f['profit']:+,.0f}</td>
<td class="num" style="color:{pc}">{f['profit_pct']:+.1f}%</td>
<td class="tags">{advice_tags}</td>
</tr>''')

    # 预警HTML
    alerts_html = ''
    if alerts:
        alert_items = []
        for a in alerts:
            ac = '#ff5252' if a['estimate_rate'] < 0 else '#00e676'
            alert_items.append(
                f'<div class="alert-item"><span class="alert-name">{a["name"]}</span> '
                f'<span class="alert-rate" style="color:{ac}">{a["estimate_rate"]:+.2f}%</span> '
                f'累计 {a["profit_pct"]:+.1f}%</div>'
            )
        alerts_html = '<div class="alerts"><h3>关注提醒</h3>' + ''.join(alert_items) + '</div>'

    # 预测HTML
    pred_html = ''
    if prediction:
        pc = '#00e676' if prediction['predict_mid'] >= 0 else '#ff5252'
        pred_html = f'''<div class="prediction">
<h3>今日盈亏预测</h3>
<div class="pred-row"><span>预估方向</span><span style="color:{pc}">{prediction['direction']}</span></div>
<div class="pred-row"><span>预估范围</span><span>¥{prediction['predict_low']:+,.0f} ~ ¥{prediction['predict_high']:+,.0f}</span></div>
<div class="pred-row"><span>预测依据</span><span>{prediction['basis']}</span></div>
</div>'''

    # 大盘HTML
    index_html = '<div class="indices">'
    for code, idx in indices.items():
        c = '#00e676' if idx['change_pct'] >= 0 else '#ff5252'
        index_html += f'<div class="idx-card"><div class="idx-name">{idx["name"]}</div><div class="idx-val">{idx["price"]:.2f}</div><div class="idx-pct" style="color:{c}">{idx["change_pct"]:+.2f}%</div></div>'
    index_html += '</div>'

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>基金监控 - {now.strftime("%m-%d %H:%M")}</title>
<style>
:root{{--bg:#0f1117;--card:#161b22;--text:#c9d1d9;--muted:#8b949e;--up:#00e676;--down:#ff5252;--accent:#58a6ff;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.6;max-width:720px;margin:0 auto;padding:16px}}
.header{{text-align:center;padding:24px 0 16px}}
.mode{{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}}
.big-num{{font-size:48px;font-weight:800;color:{color};margin:8px 0}}
.big-label{{font-size:14px;color:var(--muted)}}
.kpis{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin:20px 0}}
.kpi{{background:var(--card);border-radius:12px;padding:16px;text-align:center}}
.kpi-val{{font-size:22px;font-weight:700}}
.kpi-label{{font-size:12px;color:var(--muted);margin-top:4px}}
.indices{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}}
.idx-card{{background:var(--card);border-radius:10px;padding:12px 8px;text-align:center}}
.idx-name{{font-size:11px;color:var(--muted)}}
.idx-val{{font-size:16px;font-weight:700;margin:4px 0}}
.idx-pct{{font-size:13px;font-weight:600}}
h3{{font-size:15px;color:var(--muted);margin:20px 0 10px;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;padding:10px 8px;color:var(--muted);font-weight:500;border-bottom:1px solid #30363d}}
td{{padding:10px 8px;border-bottom:1px solid #21262d;vertical-align:top}}
.fname{{font-weight:600;font-size:13px}}
.fcode{{font-size:11px;color:var(--muted);margin-top:2px}}
.num{{font-variant-numeric:tabular-nums;font-weight:600}}
.tags{{display:flex;flex-wrap:wrap;gap:4px}}
.tag{{background:#1f2937;color:#9ca3af;font-size:10px;padding:2px 8px;border-radius:4px}}
.alerts{{background:var(--card);border-radius:12px;padding:16px;margin:16px 0}}
.alert-item{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #21262d;font-size:13px}}
.alert-item:last-child{{border-bottom:none}}
.alert-name{{font-weight:600}}
.prediction{{background:linear-gradient(135deg,#1a1f2e 0%,#161b22 100%);border-radius:12px;padding:16px;margin:16px 0;border-left:3px solid var(--accent)}}
.pred-row{{display:flex;justify-content:space-between;padding:6px 0;font-size:13px}}
.footer{{text-align:center;margin-top:24px;padding-top:16px;border-top:1px solid #21262d;font-size:11px;color:var(--muted)}}
@media(max-width:480px){{.indices{{grid-template-columns:repeat(2,1fr)}} th:nth-child(5),td:nth-child(5){{display:none}}}}
</style>
</head>
<body>
<div class="header">
<div class="mode">{mode_names.get(mode,'监控')} · {now.strftime("%m-%d %H:%M")}</div>
<div class="big-num">{arrow} ¥{total:+,.0f}</div>
<div class="big-label">今日盈亏</div>
</div>
<div class="kpis">
<div class="kpi"><div class="kpi-val" style="color:{'var(--up)' if status['total_profit']>=0 else 'var(--down)'}">¥{status["total_profit"]:+,.0f}</div><div class="kpi-label">累计盈亏</div></div>
<div class="kpi"><div class="kpi-val">{status["total_profit_pct"]:+.1f}%</div><div class="kpi-label">收益率</div></div>
<div class="kpi"><div class="kpi-val">{up}涨 {down}跌</div><div class="kpi-label">{len(funds)}只基金</div></div>
</div>
{index_html}
{pred_html}
{alerts_html}
<h3>持仓明细</h3>
<table><thead><tr><th>基金</th><th>今日涨跌</th><th>今日盈亏</th><th>累计盈亏</th><th>收益率</th><th>建议</th></tr></thead><tbody>
{chr(10).join(rows)}
</tbody></table>
<div class="footer">数据来源: 天天基金 / 东方财富 | 仅供参考不构成投资建议</div>
</body></html>'''

    filename = f'summary_{mode}_{now.strftime("%Y%m%d_%H%M")}.html'
    filepath = SUMMARIES_DIR / filename
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html)
    return filename


# ============== Bark 内容构建 ==============
def build_bark_content(status, indices, mode='close'):
    funds = [f for f in status['funds'] if f['status'] == 'ok']
    total = status['total_today_profit']
    now = datetime.now()

    mode_titles = {'morning': '上午预测', 'midday': '盘中监控', 'close': '收盘总结'}
    title = f'{mode_titles.get(mode)} {now.strftime("%m-%d %H:%M")} {total:+,.0f}元'

    lines = []
    lines.append(f'累计盈亏 ¥{status["total_profit"]:+,.0f} ({status["total_profit_pct"]:+.1f}%)')

    idx_names = {'000001': '上证', '000300': '沪深300', '399006': '创业板', '000688': '科创50'}
    idx_parts = []
    for code, idx in indices.items():
        if code in idx_names:
            arrow = '↑' if idx['change_pct'] > 0 else '↓'
            idx_parts.append(f'{idx_names[code]}{arrow}{idx["change_pct"]:+.1f}%')
    if idx_parts:
        lines.append(' | '.join(idx_parts))

    top5 = sorted(funds, key=lambda x: -abs(x['today_profit']))[:5]
    lines.append('')
    for f in top5:
        arrow = '↑' if f['today_profit'] >= 0 else '↓'
        lines.append(f'{arrow}{f["name"][:7]} ¥{f["today_profit"]:+,.0f} ({f["estimate_rate"]:+.1f}%)')

    alerts = [f for f in funds if f['estimate_rate'] <= -3 or f['estimate_rate'] >= 5 or f['profit_pct'] <= -8]
    if alerts:
        lines.append('')
        lines.append(f'关注 {len(alerts)} 只')

    body = '\n'.join(lines)
    return title, body


# ============== 主入口 ==============
def main():
    mode = 'midday'
    args = sys.argv[1:]
    for arg in args:
        if arg.startswith('--mode='):
            mode = arg.split('=')[1]

    now = datetime.now()
    print(f'🔄 [{mode}] 开始监控 - {now.strftime("%Y-%m-%d %H:%M:%S")}')

    indices = get_index_realtime()
    status = calculate_all_holdings()

    if not status['funds']:
        print('❌ 未加载到持仓数据')
        sys.exit(1)

    ok_count = len([f for f in status['funds'] if f['status'] == 'ok'])
    print(f'✅ 数据获取完成: {ok_count}/{len(status["funds"])} 只基金')

    chart_file = generate_chart_png(status, mode=mode)
    if chart_file:
        print(f'📊 PNG图表: charts/{chart_file}')

    summary_file = generate_summary_html(status, indices, mode=mode)
    print(f'📄 HTML看板: summaries/{summary_file}')

    generate_index_html()

    title, body = build_bark_content(status, indices, mode=mode)

    summary = {
        'time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'mode': mode,
        'total_market': status['total_market'],
        'total_profit': status['total_profit'],
        'total_profit_pct': status['total_profit_pct'],
        'total_today_profit': status['total_today_profit'],
        'fund_count': ok_count,
        'chart_file': chart_file,
        'summary_file': summary_file,
        'bark_title': title,
        'bark_body': body,
    }
    with open('latest_summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f'✅ 监控完成，数据已保存到 latest_summary.json')


def generate_index_html():
    summaries = sorted(SUMMARIES_DIR.glob('summary_*.html'), reverse=True)[:30]
    chart_files = sorted(CHARTS_DIR.glob('chart_*.png'), reverse=True)[:30]

    links = [f'<li><a href="summaries/{s.name}">{s.name}</a></li>' for s in summaries]
    chart_links = [f'<li><a href="charts/{c.name}">{c.name}</a></li>' for c in chart_files]

    html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>基金监控历史</title>
<style>
body{{background:#0f1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:600px;margin:0 auto;padding:20px}}
h1{{font-size:20px;margin-bottom:16px}}h2{{font-size:14px;color:#8b949e;margin:20px 0 8px}}
a{{color:#58a6ff;text-decoration:none}}a:hover{{text-decoration:underline}}
ul{{list-style:none;padding:0}}li{{padding:6px 0;border-bottom:1px solid #21262d;font-size:13px}}
.time{{color:#8b949e;font-size:12px}}
</style></head><body>
<h1>基金监控历史报告</h1>
<p class="time">最新更新: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
<h2>📄 详细报告</h2><ul>{chr(10).join(links)}</ul>
<h2>📊 图表</h2><ul>{chr(10).join(chart_links)}</ul>
</body></html>'''

    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    main()
