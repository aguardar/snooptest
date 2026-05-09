#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_report.py — 多平台 snoop filter benchmark 对比报告生成器

用法:
  # 自动以文件名（去掉 .csv）作为平台标签
  python compare_report.py skylake.csv zen3.csv icelake.csv -o report.html

  # 显式指定标签 ( label=path )
  python compare_report.py "Skylake-SP=a.csv" "Zen3=b.csv" -o report.html

  # 提供 TSC 频率（GHz），将 cycles 折算为 ns 同时显示
  python compare_report.py *.csv --tsc-ghz 2.5 -o report.html
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go


# --------------------------------------------------------------------------
# I/O 与数据加载
# --------------------------------------------------------------------------
def parse_inputs(items):
    parsed = []
    for it in items:
        if '=' in it and not os.path.exists(it):
            label, path = it.split('=', 1)
        else:
            path = it
            label = Path(it).stem
        parsed.append((label.strip(), path.strip()))
    return parsed


REQUIRED_COLS = {'role', 'core', 'lines', 'iters',
                 'min', 'p50', 'p95', 'p99', 'max', 'avg'}


def load_data(inputs):
    frames = []
    for label, path in inputs:
        if not os.path.exists(path):
            print(f"[WARN] 文件不存在，跳过: {path}", file=sys.stderr)
            continue
        df = pd.read_csv(path)
        miss = REQUIRED_COLS - set(df.columns)
        if miss:
            print(f"[WARN] {path} 缺少列 {miss}，跳过", file=sys.stderr)
            continue
        df['platform'] = label
        df['source_file'] = os.path.basename(path)
        frames.append(df)
        print(f"[OK]  loaded {label:20s} <- {path}  rows={len(df)}")
    if not frames:
        sys.exit("ERROR: 无有效输入文件")
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------
# 分析函数
# --------------------------------------------------------------------------
def detect_cliff(lines, p50, threshold=2.0):
    """检测 p50 相对基线（最小 3 个点的中位数）首次跨过 threshold 倍的位置。"""
    if len(lines) < 4:
        return None
    arr = np.asarray(p50, dtype=float)
    ln = np.asarray(lines, dtype=int)
    order = np.argsort(ln)
    arr, ln = arr[order], ln[order]
    baseline = np.median(arr[:3])
    if baseline <= 0:
        return None
    for i in range(3, len(arr)):
        if arr[i] > threshold * baseline:
            return int(ln[i])
    return None


def cliff_summary_table(df):
    rows = []
    sub = df[df['role'] == 'writer']
    for plat, g in sub.groupby('platform'):
        g = g.sort_values('lines')
        baseline = float(np.median(g['p50'].values[:3])) if len(g) >= 3 else float('nan')
        peak = float(g['p50'].max())
        c2 = detect_cliff(g['lines'].values, g['p50'].values, 2.0)
        c3 = detect_cliff(g['lines'].values, g['p50'].values, 3.0)
        c10 = detect_cliff(g['lines'].values, g['p50'].values, 10.0)
        rows.append({
            '平台': plat,
            'reader 数': int(g['readers'].iloc[0]) if 'readers' in g else '-',
            '基线 p50 (cycles)': f'{baseline:,.0f}',
            '峰值 p50 (cycles)': f'{peak:,.0f}',
            '放大倍数': f'{peak/baseline:,.1f}×' if baseline > 0 else '-',
            'cliff@2× (bytes)': f'{c2*64:,}' if c2 else '未检出',
            'cliff@3× (bytes)': f'{c3*64:,}' if c3 else '未检出',
            'cliff@10× (bytes)': f'{c10*64:,}' if c10 else '未检出',
        })
    return pd.DataFrame(rows)


def per_platform_summary(df):
    rows = []
    for plat, g in df.groupby('platform'):
        w = g[g['role'] == 'writer']
        r = g[g['role'] == 'reader']
        rows.append({
            '平台': plat,
            'reader 数': int(g['readers'].iloc[0]) if 'readers' in g else '-',
            'lines 取样点数': g['lines'].nunique(),
            '最小 lines': int(g['lines'].min()),
            '最大 lines': int(g['lines'].max()),
            '最大 WS (KB)': f'{g["lines"].max()*64/1024:,.0f}',
            'writer 行数': len(w),
            'reader 行数': len(r),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 图表
# --------------------------------------------------------------------------
PALETTE = ['#2563eb', '#dc2626', '#059669', '#d97706',
           '#7c3aed', '#0891b2', '#be185d', '#65a30d',
           '#475569', '#ea580c']


def color_map(platforms):
    return {p: PALETTE[i % len(PALETTE)] for i, p in enumerate(platforms)}


def fig_writer_curve(df, metric, cmap, tsc_ghz):
    fig = go.Figure()
    sub = df[df['role'] == 'writer']
    for plat, g in sub.groupby('platform'):
        g = g.sort_values('lines')
        x = g['lines'] * 64
        y = g[metric]
        ns = (y / tsc_ghz) if tsc_ghz else None
        ht = (f'<b>{plat}</b><br>WS=%{{x:,}} B<br>{metric}=%{{y:,.0f}} cyc'
              + (f' (≈%{{customdata:.1f}} ns)' if tsc_ghz else '')
              + '<extra></extra>')
        fig.add_trace(go.Scatter(
            x=x, y=y,
            customdata=ns,
            mode='lines+markers', name=plat,
            line=dict(color=cmap[plat], width=2),
            marker=dict(size=6),
            hovertemplate=ht,
        ))
    fig.update_layout(
        title=f'Writer fan-out 延迟（{metric}）随 working-set 变化',
        xaxis=dict(title='Working set (bytes, log)', type='log'),
        yaxis=dict(title=f'{metric} (TSC cycles, log)', type='log'),
        template='plotly_white', height=500,
        legend=dict(orientation='h', y=-0.18),
    )
    return fig


def fig_reader_band(df, metric, cmap, tsc_ghz):
    fig = go.Figure()
    sub = df[df['role'] == 'reader']
    agg = (sub.groupby(['platform', 'lines'])[metric]
              .agg(median='median', q_lo=lambda s: s.quantile(0.1),
                   q_hi=lambda s: s.quantile(0.9))
              .reset_index())
    for plat, g in agg.groupby('platform'):
        g = g.sort_values('lines')
        x = g['lines'] * 64
        c = cmap[plat]
        # band
        fig.add_trace(go.Scatter(
            x=list(x) + list(x[::-1]),
            y=list(g['q_hi']) + list(g['q_lo'][::-1]),
            fill='toself', line=dict(width=0),
            fillcolor=c, opacity=0.15,
            name=f'{plat} (10–90% 跨核区间)',
            hoverinfo='skip', showlegend=False,
        ))
        fig.add_trace(go.Scatter(
            x=x, y=g['median'], mode='lines+markers',
            line=dict(color=c, width=2), marker=dict(size=6),
            name=plat,
            hovertemplate=f'<b>{plat}</b><br>WS=%{{x:,}} B<br>'
                          f'reader {metric} 中位数=%{{y:,.0f}} cyc<extra></extra>',
        ))
    fig.update_layout(
        title=f'Reader 延迟（{metric}）—— 跨 reader 核的中位数与 10/90 分位带',
        xaxis=dict(title='Working set (bytes, log)', type='log'),
        yaxis=dict(title=f'{metric} (TSC cycles, log)', type='log'),
        template='plotly_white', height=500,
        legend=dict(orientation='h', y=-0.18),
    )
    return fig


def fig_tail_ratio(df, cmap):
    fig = go.Figure()
    sub = df[df['role'] == 'writer'].copy()
    sub['ratio'] = sub['p99'] / sub['p50']
    for plat, g in sub.groupby('platform'):
        g = g.sort_values('lines')
        fig.add_trace(go.Scatter(
            x=g['lines'] * 64, y=g['ratio'],
            mode='lines+markers', name=plat,
            line=dict(color=cmap[plat], width=2),
            marker=dict(size=6),
            hovertemplate=f'<b>{plat}</b><br>WS=%{{x:,}} B<br>'
                          f'p99/p50=%{{y:.2f}}<extra></extra>',
        ))
    fig.add_hline(y=2.0, line_dash='dot', line_color='gray',
                  annotation_text='2× 抖动参考线', annotation_position='top right')
    fig.update_layout(
        title='Writer 尾延迟放大比 p99 / p50（值越高表示抖动越剧烈）',
        xaxis=dict(title='Working set (bytes, log)', type='log'),
        yaxis=dict(title='p99 / p50'),
        template='plotly_white', height=420,
        legend=dict(orientation='h', y=-0.22),
    )
    return fig


def fig_normalized_curve(df, cmap):
    """每个平台用其最小 working-set 的 p50 作归一化基线。"""
    fig = go.Figure()
    sub = df[df['role'] == 'writer']
    for plat, g in sub.groupby('platform'):
        g = g.sort_values('lines')
        baseline = np.median(g['p50'].values[:3])
        if baseline <= 0:
            continue
        fig.add_trace(go.Scatter(
            x=g['lines'] * 64, y=g['p50'] / baseline,
            mode='lines+markers', name=plat,
            line=dict(color=cmap[plat], width=2),
            marker=dict(size=6),
            hovertemplate=f'<b>{plat}</b><br>WS=%{{x:,}} B<br>'
                          f'normalized=%{{y:.2f}}×<extra></extra>',
        ))
    fig.add_hline(y=2.0, line_dash='dot', line_color='gray',
                  annotation_text='2× cliff 阈值', annotation_position='top right')
    fig.update_layout(
        title='归一化 writer p50（相对各自最小 WS 基线），便于对比 cliff 形态',
        xaxis=dict(title='Working set (bytes, log)', type='log'),
        yaxis=dict(title='p50 / baseline (log)', type='log'),
        template='plotly_white', height=450,
        legend=dict(orientation='h', y=-0.20),
    )
    return fig


def fig_per_core_heatmap(df, platform):
    sub = df[(df['platform'] == platform) & (df['role'] == 'reader')]
    if sub.empty:
        return None
    pivot = sub.pivot_table(index='core', columns='lines',
                            values='p50', aggfunc='mean')
    pivot = pivot.reindex(sorted(pivot.columns), axis=1)
    z = np.log10(pivot.values.astype(float))
    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=[f'{c*64}' for c in pivot.columns],
        y=[f'core {c}' for c in pivot.index],
        colorscale='Viridis',
        colorbar=dict(title='log10(p50)'),
        hovertemplate='WS=%{x} B<br>%{y}<br>log10(p50 cyc)=%{z:.2f}<extra></extra>',
    ))
    fig.update_layout(
        title=f'[{platform}] 每个 reader 核的 p50 延迟热图（log10 cycles）',
        xaxis_title='Working set (bytes)',
        yaxis_title='Reader core',
        template='plotly_white',
        height=max(320, 22 * len(pivot.index)),
    )
    return fig


def fig_per_core_curves(df, platform, cmap_unused):
    """单平台下，每个 reader 核一条 p50 曲线，用于看 NUMA / die 拓扑差异。"""
    sub = df[(df['platform'] == platform) & (df['role'] == 'reader')]
    if sub.empty:
        return None
    fig = go.Figure()
    cores = sorted(sub['core'].unique())
    n = len(cores)
    for i, c in enumerate(cores):
        g = sub[sub['core'] == c].sort_values('lines')
        # 用连续色盘
        hue = i / max(n - 1, 1)
        color = f'hsl({int(hue*330)}, 70%, 45%)'
        fig.add_trace(go.Scatter(
            x=g['lines'] * 64, y=g['p50'],
            mode='lines+markers', name=f'core {c}',
            line=dict(color=color, width=1.5),
            marker=dict(size=4),
            hovertemplate=f'core {c}<br>WS=%{{x:,}} B<br>p50=%{{y:,.0f}} cyc<extra></extra>',
        ))
    fig.update_layout(
        title=f'[{platform}] 各 reader 核 p50 曲线（识别同 die / 跨 die / 跨 socket）',
        xaxis=dict(title='Working set (bytes, log)', type='log'),
        yaxis=dict(title='p50 (TSC cycles, log)', type='log'),
        template='plotly_white', height=520,
        legend=dict(font=dict(size=10)),
    )
    return fig


# --------------------------------------------------------------------------
# HTML 渲染
# --------------------------------------------------------------------------
PLOTLY_CDN = 'https://cdn.plot.ly/plotly-2.32.0.min.js'

CSS = """
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
       max-width: 1280px; margin: 0 auto; padding: 28px 32px; color: #1f2937;
       line-height: 1.75; background: #f8fafc; }
h1 { border-bottom: 3px solid #2563eb; padding-bottom: 10px; font-size: 28px; }
h2 { border-left: 5px solid #2563eb; padding-left: 14px; margin-top: 48px;
     background: #fff; padding-top: 6px; padding-bottom: 6px; border-radius: 0 4px 4px 0; }
h3 { color: #374151; margin-top: 28px; }
h4 { color: #4b5563; }
p, li { font-size: 15px; }
.meta { color: #6b7280; font-size: 13px; }
.tip { background: #eff6ff; border-left: 4px solid #2563eb; padding: 12px 18px;
       margin: 16px 0; border-radius: 4px; font-size: 14px; }
.warn { background: #fef3c7; border-left: 4px solid #f59e0b; padding: 12px 18px;
       margin: 16px 0; border-radius: 4px; font-size: 14px; }
.ok   { background: #ecfdf5; border-left: 4px solid #10b981; padding: 12px 18px;
       margin: 16px 0; border-radius: 4px; font-size: 14px; }
table { border-collapse: collapse; width: 100%; margin: 14px 0;
        background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.06); font-size: 14px; }
th, td { border: 1px solid #e5e7eb; padding: 8px 12px; text-align: left; }
th { background: #f3f4f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafafa; }
.chart { background: #fff; padding: 12px; margin: 18px 0;
         border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
code, pre { background: #f3f4f6; padding: 2px 6px; border-radius: 3px;
            font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 13px; }
pre { padding: 12px 16px; overflow-x: auto; }
.footer { margin-top: 64px; padding-top: 18px; border-top: 1px solid #e5e7eb;
         color: #9ca3af; font-size: 12px; }
.toc { background: #fff; padding: 14px 22px; border-radius: 6px;
       box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.toc ol { margin: 6px 0; }
.toc a { color: #2563eb; text-decoration: none; }
.toc a:hover { text-decoration: underline; }
details { background: #fff; border-radius: 6px; padding: 8px 14px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin: 10px 0; }
summary { cursor: pointer; font-weight: 600; color: #1d4ed8; }
.kvbox { display: flex; flex-wrap: wrap; gap: 12px; margin: 14px 0; }
.kv { background: #fff; border-radius: 6px; padding: 12px 18px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.06); flex: 1 1 180px; }
.kv .k { color: #6b7280; font-size: 12px; }
.kv .v { font-weight: 700; font-size: 18px; color: #1f2937; }
"""


def df_to_html_table(df):
    return df.to_html(index=False, escape=False, border=0)


def fig_div(fig):
    return ('<div class="chart">'
            + fig.to_html(full_html=False, include_plotlyjs=False,
                          config={'displaylogo': False})
            + '</div>')


def render_report(df, out_path, tsc_ghz=None):
    platforms = sorted(df['platform'].unique())
    cmap = color_map(platforms)
    n_platforms = len(platforms)
    n_lines = df['lines'].nunique()
    n_rows = len(df)

    # ---------- 头部 ----------
    parts = []
    parts.append(f'<h1>Snoop Filter Benchmark · 多平台对比报告</h1>')
    parts.append(f'<div class="meta">生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
                 f' · 平台数：{n_platforms} · 总行数：{n_rows}'
                 + (f' · TSC≈{tsc_ghz} GHz（用于 ns 折算）' if tsc_ghz else '')
                 + '</div>')

    # ---------- 关键指标卡片 ----------
    parts.append('<div class="kvbox">')
    for p in platforms:
        g = df[(df['platform'] == p) & (df['role'] == 'writer')].sort_values('lines')
        if g.empty:
            continue
        baseline = np.median(g['p50'].values[:3])
        peak = g['p50'].max()
        cliff = detect_cliff(g['lines'].values, g['p50'].values, 2.0)
        amp = peak / baseline if baseline > 0 else 0
        cliff_str = f'{cliff*64/1024:.0f} KB' if cliff else '未触发'
        parts.append(
            f'<div class="kv" style="border-top:4px solid {cmap[p]}">'
            f'<div class="k">{p}</div>'
            f'<div class="v">{baseline:,.0f} → {peak:,.0f} cyc</div>'
            f'<div class="k">放大 {amp:,.1f}× · 2× cliff @ {cliff_str}</div>'
            f'</div>'
        )
    parts.append('</div>')

    # ---------- 目录 ----------
    parts.append("""
<div class="toc"><b>目录</b>
<ol>
  <li><a href="#sec-principle">测试原理</a></li>
  <li><a href="#sec-columns">数据列含义</a></li>
  <li><a href="#sec-overview">平台概览</a></li>
  <li><a href="#sec-compare">跨平台对比图</a></li>
  <li><a href="#sec-cliff">Cliff 点检测与放大倍数</a></li>
  <li><a href="#sec-perplat">单平台详细分析</a></li>
  <li><a href="#sec-conclusion">如何得出分析结论</a></li>
  <li><a href="#sec-raw">原始数据表</a></li>
</ol></div>
""")

    # ---------- 1. 原理 ----------
    parts.append('<h2 id="sec-principle">1. 测试原理</h2>')
    parts.append("""
<p>本测试用于评估多核 CPU <b>cache coherence 子系统</b>（snoop filter / directory / mesh interconnect）
在不同 working-set 大小下的行为，特别是：当被多个 reader 共享的 cache line 总量超过 snoop filter 容量时，
是否触发 <b>back-invalidation</b>（回写失效），从而引入显著的延迟悬崖（"cliff"）。</p>

<h3>线程角色与流程</h3>
<ul>
  <li><b>Writer（默认 core 0）</b>：每轮迭代对 N 条 cache line 依次写入新版本号，
      记录 <code>t_send = rdtsc()</code>，等待所有 reader 在共享 slot 中 ack 完毕，
      记录 <code>t_done</code>，得到 <b>fan-out 延迟</b> = <code>t_done − t_send</code>。</li>
  <li><b>Readers（其余所有核）</b>：在 N 条 cache line 上忙等版本号变化，
      看到最后一条更新后写入 ack，并记录 <code>t_recv</code>，得到 <b>write-to-observe</b>
      延迟 = <code>t_recv − t_send</code>（依赖 invariant TSC 同 socket 同步）。</li>
</ul>

<h3>为什么能观察 snoop filter 容量</h3>
<p>当 N × 64 B 远小于 LLC 中 snoop filter 能跟踪的容量时，所有 reader 的 cache line
都能被精确跟踪，writer 的写入仅对持有副本的 reader 发出失效——延迟低且抖动小。</p>
<p>当 N 增大到超过 snoop filter 跟踪容量后，filter 必须驱逐已跟踪的条目；
被驱逐的 line 在 reader 端被强制失效（即使逻辑上仍被使用），writer 的下一次写入
会落到 LLC/内存路径，触发跨 die / 跨 socket snoop 广播——延迟显著上升、p99 抖动放大。
这就是图中观察到的 <b>cliff（性能悬崖）</b>。</p>

<div class="tip">不同微架构的 snoop filter 实现差异巨大：Intel Skylake-SP/Ice Lake-SP 使用
mesh + per-CHA snoop filter；AMD Zen 使用 CCX/CCD 内 L3 + 跨 CCD probe filter；
ARM Neoverse 使用 SLC + snoop filter。<b>cliff 出现的位置即间接反映了 filter 的跟踪容量</b>。</div>
""")

    # ---------- 2. 数据列 ----------
    parts.append('<h2 id="sec-columns">2. CSV 数据列含义</h2>')
    parts.append("""
<table>
<tr><th>列</th><th>含义</th><th>单位</th></tr>
<tr><td><code>tag</code></td><td>本次运行标签（通常为 <code>lines=N</code>）</td><td>字符串</td></tr>
<tr><td><code>role</code></td><td><code>writer</code> 或 <code>reader</code></td><td>—</td></tr>
<tr><td><code>core</code></td><td>线程绑定的逻辑核号</td><td>整数</td></tr>
<tr><td><code>readers</code></td><td>本次运行 reader 总数</td><td>整数</td></tr>
<tr><td><code>lines</code></td><td>working-set 大小（cache line 数，× 64B = 字节）</td><td>整数</td></tr>
<tr><td><code>iters</code></td><td>统计阶段迭代次数（不含 warmup）</td><td>整数</td></tr>
<tr><td><code>min/p50/p95/p99/max/avg</code></td><td>该线程所有迭代延迟的统计量</td><td>TSC cycles</td></tr>
</table>
<p><b>writer 的延迟</b>是"广播 + 全部 ack"的 fan-out 时间；<b>reader 的延迟</b>是单个 reader 看到最后一行更新的 write-to-observe 时间。</p>
""")

    # ---------- 3. 平台概览 ----------
    parts.append('<h2 id="sec-overview">3. 平台概览</h2>')
    parts.append(df_to_html_table(per_platform_summary(df)))

    # ---------- 4. 对比图 ----------
    parts.append('<h2 id="sec-compare">4. 跨平台对比图</h2>')
    parts.append('<h3>4.1 Writer fan-out 延迟（中位数 p50）</h3>')
    parts.append('<p>这是核心指标。曲线水平段对应 snoop filter 命中区间；'
                 '斜率显著上升的位置即 <b>cliff 点</b>。曲线越靠下表示 coherence 路径越快。</p>')
    parts.append(fig_div(fig_writer_curve(df, 'p50', cmap, tsc_ghz)))

    parts.append('<h3>4.2 Writer 尾延迟（p99）</h3>')
    parts.append('<p>p99 反映最差 1% 情况下的延迟。snoop filter 压力大时尾延迟会显著放大，'
                 '是判断系统是否仍在"线性可扩展区"的重要信号。</p>')
    parts.append(fig_div(fig_writer_curve(df, 'p99', cmap, tsc_ghz)))

    parts.append('<h3>4.3 Reader write-to-observe 延迟</h3>')
    parts.append('<p>展示跨所有 reader 核的中位数曲线，附带 10–90 分位带。'
                 '区间越宽说明不同 reader 核（如同 die / 跨 die / 跨 socket）之间差异越大。</p>')
    parts.append(fig_div(fig_reader_band(df, 'p50', cmap, tsc_ghz)))

    parts.append('<h3>4.4 尾延迟放大比 p99 / p50</h3>')
    parts.append('<p>该比值 > 2 通常意味着出现了显著抖动源（snoop 冲突、调度、C-state）。'
                 '在 cliff 区域附近此比值往往率先抬升，可作为 cliff 的"前兆指标"。</p>')
    parts.append(fig_div(fig_tail_ratio(df, cmap)))

    parts.append('<h3>4.5 归一化曲线（消除绝对频率差异）</h3>')
    parts.append('<p>每条曲线除以自身在最小 working-set 时的 p50 基线，'
                 '使不同平台在同一坐标系下对比 <b>cliff 形态与位置</b>，'
                 '避免被 TSC 频率或基础延迟差异干扰判断。</p>')
    parts.append(fig_div(fig_normalized_curve(df, cmap)))

    # ---------- 5. cliff ----------
    parts.append('<h2 id="sec-cliff">5. Cliff 点检测与放大倍数</h2>')
    parts.append('<p>使用启发式规则：当 writer p50 首次跨过基线（最小 3 个工作集 p50 中位数）的'
                 ' 2× / 3× / 10× 阈值时，记录对应 working-set 字节数。</p>')
    parts.append(df_to_html_table(cliff_summary_table(df)))
    parts.append("""
<div class="tip"><b>解读：</b>
<ul>
<li><b>cliff@2×</b> 通常代表 <b>L1/L2 私有缓存或近端 snoop filter 容量耗尽</b>。</li>
<li><b>cliff@3×–10×</b> 之间往往是 <b>LLC slice / mesh snoop filter 容量边界</b>。</li>
<li><b>cliff@10× 以上</b> 已进入 <b>跨 socket / 内存路径</b>。</li>
<li><b>放大倍数</b> 越大说明 coherence 设计在压力下退化越剧烈，对延迟敏感型应用越不友好。</li>
</ul></div>
""")

    # ---------- 6. 单平台详细 ----------
    parts.append('<h2 id="sec-perplat">6. 单平台详细分析</h2>')
    parts.append('<p>下面每个折叠块展示该平台的 per-core 视图，便于分析 die / NUMA / 拓扑相关的延迟差异。</p>')
    for p in platforms:
        parts.append(f'<details open><summary>平台：{p}</summary>')
        f1 = fig_per_core_heatmap(df, p)
        f2 = fig_per_core_curves(df, p, cmap)
        if f1: parts.append(fig_div(f1))
        if f2: parts.append(fig_div(f2))
        # 展示该平台 writer 关键统计
        w = df[(df['platform'] == p) & (df['role'] == 'writer')] \
              .sort_values('lines')[['lines', 'core', 'min', 'p50', 'p95', 'p99', 'max', 'avg']]
        w = w.copy()
        w['bytes'] = w['lines'] * 64
        w = w[['lines', 'bytes', 'core', 'min', 'p50', 'p95', 'p99', 'max', 'avg']]
        parts.append('<h4>Writer 统计明细</h4>')
        parts.append(df_to_html_table(w))
        parts.append('</details>')

    # ---------- 7. 结论指南 ----------
    parts.append('<h2 id="sec-conclusion">7. 如何得出分析结论</h2>')
    parts.append("""
<div class="ok"><b>建议的分析步骤：</b>
<ol>
  <li><b>看 4.1 与 4.5</b>：归一化曲线最先跨过 2× 的工作集字节数即近端 snoop filter 容量，
      跨过 10× 的位置即外层 coherence 域容量。</li>
  <li><b>看 4.4</b>：在 cliff 之前若 p99/p50 已 &gt; 2，说明系统抖动源不仅是 snoop，
      还可能含有 C-state、SMI、频率切换；建议固定频率 + 关闭 turbo 重测。</li>
  <li><b>看 4.3</b>：若 reader 10–90 分位带很宽，说明<b>拓扑差异显著</b>（跨 die / 跨 socket）；
      在 6 节的 per-core 热图中可进一步定位"远端 reader"。</li>
  <li><b>跨平台对比</b>：<b>水平段更低</b>表示基础 coherence 路径延迟更小（mesh hop / die-to-die fabric 更快）；
      <b>cliff 更靠右</b>表示 snoop filter 容量更大；<b>放大倍数更小</b>表示退化更平滑、对真实工作负载更友好。</li>
  <li><b>同 socket 测试</b>结论才直接可比；跨 socket 时请先用 <code>lscpu -e</code> 与 <code>numactl -H</code> 对齐拓扑，
      再判断 cliff 是否来自跨 socket 路径。</li>
</ol></div>

<div class="warn"><b>注意事项：</b>
<ul>
  <li>请确认 <code>invariant_tsc</code> 已启用，且 writer 与 reader 同 socket，否则 reader 延迟数值不可信。</li>
  <li>SMT（Hyper-Threading）开启时，writer 与其 sibling 的"reader"走 L1/L2，并非真正的跨核 snoop；
      建议在测试前关闭 SMT 或排除 sibling 核。</li>
  <li>大 working-set（例如 &gt; 32 MB）可能被 LLC 完全踢出，曲线右段同时混合了 DRAM 访问开销，
      可与 <code>perf stat -e LLC-load-misses</code> 一起观察。</li>
</ul></div>
""")

    # ---------- 8. 原始数据 ----------
    parts.append('<h2 id="sec-raw">8. 原始数据表</h2>')
    parts.append('<p>下方为合并后的全部数据（按平台、role、lines、core 排序）。'
                 '可直接在浏览器中通过 Ctrl/Cmd+F 检索。</p>')
    show = df.sort_values(['platform', 'role', 'lines', 'core']).copy()
    show['bytes'] = show['lines'] * 64
    cols = ['platform', 'role', 'core', 'readers', 'lines', 'bytes',
            'iters', 'min', 'p50', 'p95', 'p99', 'max', 'avg']
    cols = [c for c in cols if c in show.columns]
    parts.append('<details><summary>展开 / 收起（共 {} 行）</summary>'.format(len(show)))
    parts.append(df_to_html_table(show[cols]))
    parts.append('</details>')

    parts.append('<div class="footer">由 compare_report.py 生成。'
                 'Plotly.js 图表为本地交互渲染，可缩放 / 框选 / 隐藏图例项。</div>')

    body = '\n'.join(parts)
    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Snoop Filter Benchmark 对比报告</title>
<script src="{PLOTLY_CDN}"></script>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'\n[OK] 报告已生成: {out_path}  (size={os.path.getsize(out_path)/1024:.1f} KB)')


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description='多平台 snoop filter benchmark 对比报告生成器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('inputs', nargs='+',
                    help='CSV 路径，可选写成 label=path 指定平台标签')
    ap.add_argument('-o', '--output', default='snoop_report.html',
                    help='输出 HTML 文件路径（默认: snoop_report.html）')
    ap.add_argument('--tsc-ghz', type=float, default=None,
                    help='TSC 频率（GHz），用于 hover 中显示 ns 折算')
    args = ap.parse_args()

    inputs = parse_inputs(args.inputs)
    df = load_data(inputs)
    render_report(df, args.output, tsc_ghz=args.tsc_ghz)


if __name__ == '__main__':
    main()
