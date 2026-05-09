真正能评估 Snoop Table/Filter 性能的方法
Snoop filter 的性能特征只在以下条件出现：
1.	工作集大小（cache line 数）扫描：从远小于 snoop filter 容量扫到远大于。Intel Skylake X / Cascade Lake 上 snoop filter 的有效 entry 数 ≈ 每个 LLC slice 的若干倍 LLC ways（典型 ~1.5× 2× LLC 行数）。当 working set 越过这个阈值，会出现明显的"性能悬崖"。
2.	多核共享同一组 cache line，让这些行在 snoop filter 中被多核 valid bit 标记。
3.	流式访问强制 snoop filter 频繁查表/插入/驱逐。

results.csv 各列含义说明
测试程序每次运行会向 CSV 中追加若干行（writer 1 行 + 每个 reader 1 行）。每一行代表某个线程（writer 或某个 reader）在某次配置下的延迟统计。
列定义
列名	含义	单位
tag	本次运行的标签，由脚本传入（如 lines=64），用于区分不同 working-set 大小的扫描点	字符串
role	线程角色：writer 表示 core A（写者），reader 表示某一个读者线程	字符串
core	该线程被 pin 到的逻辑核号（与 lscpu -e 里的 CPU id 对应）	整数
readers	本次运行中 reader 的总数（即除 writer 外的核数）	整数
lines	本次运行中 writer 每轮迭代写入的 cache line 数，也就是 working-set 大小（×64B = 字节数）	整数
iters	统计阶段的迭代次数（不含 warmup）	整数
min	该线程所有迭代延迟的最小值	TSC cycles
p50	中位数延迟（50 分位）	TSC cycles
p95	95 分位延迟（剔除最差 5% 的"尾延迟"指标）	TSC cycles
p99	99 分位延迟（尾延迟，常用于发现抖动）	TSC cycles
max	最大延迟	TSC cycles
avg	平均延迟	TSC cycles
各 role 下"延迟"具体指什么
•	role = writer：从 writer 写下第一个 cache line（记录 t_send）开始，到所有 readers 都 ack 收到为止的总时间。即"广播 + 全部 ack"的 fan-out 延迟，反映该轮所有相关 cache line 在 snoop filter / coherence fabric 中完成状态切换的总开销。
•	role = reader：从 writer 的 t_send（reader 从 slot 中读取到的）到该 reader 自己看到最后一行更新（t_recv）的时间。即单个 reader 的 write to observe 延迟。注意这依赖 writer 与 reader 间 TSC 同步（同 socket、invariant_tsc 时通常成立）。
cycles → 纳秒换算
TSC 在现代 Intel/AMD 上以标称基频（不是当前运行频率）计数。换算公式：
tns=cyclesfTSC,GHzt_{ns} = \frac{\text{cycles}}{f_{\text{TSC,GHz}}}tns=fTSC,GHzcycles
例如标称 2.5 GHz 的 CPU，p50 = 500 cycles ≈ 200 ns。可用 cat /proc/cpuinfo | grep 'model name' 中的频率，或 dmesg | grep -i 'tsc:.*MHz' 确认 TSC 频率。
解读建议
•	把同一 role、同一 core 的所有行按 lines 排序，绘制 p50（或 avg）随 bytes = lines * 64 的曲线（log log），观察是否出现"性能悬崖"——这就是 snoop filter 容量耗尽、开始出现 back invalidation 的拐点。
•	比较各 reader 的 p50 差异：靠近 writer（在 ring/mesh 上路由距离短）的 core 通常更低，可以反推 die 上的拓扑。
•	关注 p99 / p50 的比值：snoop filter 压力大时尾延迟会显著放大。
如果你额外打开了 RAW_DUMP=raw.csv 环境变量，则 raw.csv 里是逐迭代的原始数据，列含义为：
列名	含义
tag	同上
role	同上
core	同上
iter	第几次有效迭代（从 0 开始，已剔除 warmup）
latency_cycles	本次迭代的延迟（TSC cycles）
可用于做直方图、CDF 或时间序列分析（例如查看是否随时间漂移，识别 C-state / 频率切换造成的抖动）。

为什么 writer 延迟随 working-set（cache line 数 N）增大也变大？
这其实是这个 benchmark 的主信号——它就是为了画出这条曲线而设计的。曲线的形状包含了 cache 层级、snoop filter、TLB、prefetcher 的全部信息。下面拆开看。
1. 最朴素的一项：写 N 条线，本身就是 O(N)
writer 每轮要执行：
for (i = 0; i < N; i++) lines[i].version++;
这就是 N 次写操作。即使所有 line 都在 L1 里、没有任何 coherence 开销，延迟也是：
Twriter  ≥  N⋅tstoreT_{\text{writer}} \;\geq\; N \cdot t_{\text{store}}Twriter≥N⋅tstore
其中 tstoret_{\text{store}}tstore 是一次 L1 store 的成本（几个 cycle）。所以曲线的最底部本来就是一条斜率为 tstoret_{\text{store}}tstore 的直线，这是基线，不是异常。
真正有意思的是：当 N 跨过某些阈值时，斜率会突然跳一个台阶。这些台阶就是 cache / TLB / snoop filter 的容量边界。
2. Cache 层级的 cliff（逐级跌落）
随着 N 增大，working-set 依次溢出 L1 → L2 → LLC → DRAM。每跨一级，单次访问成本就跳一档：
Working-set 大小	每条 line 的访问成本（典型值）	写 N 条的总延迟
N⋅64B≤N \cdot 64\text{B} \leN⋅64B≤ L1 (∼\sim∼32–48 KB)	∼\sim∼4–5 cycle	O(N)O(N)O(N) 斜率最缓
≤\le≤ L2 (∼\sim∼1–2 MB)	∼\sim∼12–15 cycle	斜率 ×3
≤\le≤ LLC (∼\sim∼数十 MB)	∼\sim∼40–80 cycle	斜率 ×10
>>> LLC	∼\sim∼200–400 cycle (DRAM)	斜率 ×50 以上
所以曲线长这样（log-log）：
T
│              ╱─── DRAM 平原
│            ╱
│      ╲___╱  ← LLC cliff
│    ╲╱
│  ╲╱   ← L2 cliff
│╲╱
└──────────────── N
   L1   L2    LLC
每一个 cliff 的位置就是对应 cache 容量除以 64B（再除以 way 数考虑 associativity）。
3. Coherence 状态翻转：写 N 条线 = N 次 RFO
每次 writer 写一条 reader 们曾经读过的 line，硬件必须做一次 Read-For-Ownership (RFO)：
1.	把 line 拉到 writer 的 L1/L2，置为 M（Modified）；
2.	给所有持有 Shared 副本的 reader 发 invalidate；
3.	收集 invalidate-ack。
N 越大，RFO 总数越多，coherence 流量是 O(N⋅K)O(N \cdot K)O(N⋅K)，其中 K 是 reader 数。这部分流量都要经过 mesh / ring，在带宽不变的情况下，延迟自然随 N 线性增长（甚至超线性，当接近带宽上限时排队效应放大）。
4. Snoop filter 容量 cliff（最关键的信号）
这是这个 benchmark 真正想测的东西。
现代 Intel/AMD CPU 有一个叫 snoop filter / coherence directory 的结构，用来跟踪"哪些 cache line 被哪些核共享"。它的容量是有限的，通常和 LLC 容量相当或略小。
当 working-set 中正在被多核共享的 line 数超过 snoop filter 容量时，硬件无法记录新条目，必须驱逐一个旧条目。驱逐的代价不是简单地丢掉记录——为了保证一致性，它必须强制 invalidate 所有持有该 line 的核中的副本（这叫 back-invalidation 或 snoop-filter-induced invalidation）。
后果是：
•	后续 reader 再访问这条 line 时变成 L2/LLC miss（哪怕 line 还躺在某个 reader 的 L1 里也被强行作废了）；
•	writer 写这条 line 时，因为没人持有，反而 RFO 更轻——但 reader 端访问被显著拖慢；
•	writer 等 ack 时，reader 要先把 line 从更远的层级（LLC 甚至 DRAM）拉回来，再读 version，再写 ack——writer 看到的 ack 延迟暴增。
所以 snoop filter cliff 通常表现为：
在 working-set 还没塞满 LLC 时，曲线就出现一个明显的台阶（因为 snoop filter 比 LLC 小，或者 sharing degree 让 effective capacity 更小）。
这个 cliff 的位置 = snoop_filter_entries / K（K 是共享者数），所以 K 越大、cliff 越早——这正是上一条回复里提到的"K 增加时 cliff 左移"现象的根本原因。
5. TLB 容量 cliff
写 N 条线如果跨越很多 page（4 KB page 下，每页只能放 64 条 cache line），N 增大会让 working-set 占用的 page 数 P=⌈N⋅64/4096⌉=⌈N/64⌉P = \lceil N \cdot 64 / 4096 \rceil = \lceil N / 64 \rceilP=⌈N⋅64/4096⌉=⌈N/64⌉ 也增大。
•	L1 dTLB 通常 64–128 项；
•	L2 (STLB) 通常 1.5K–2K 项；
•	超过后每次访问都要 walk page table，至少一次 LLC 访问，最坏 4 次 DRAM 访问。
所以你常会在 N≈64×TLB 大小N \approx 64 \times \text{TLB 大小}N≈64×TLB 大小 处看到一个额外的 cliff，它独立于 cache cliff。用 hugepage（2 MB）可以把这个 cliff 推得很远，便于把 TLB 效应和 cache 效应分开看。
6. Hardware prefetcher 的"假救场"和"突然失效"
prefetcher 在 N 较小、访问模式规整时会显著降低延迟，让曲线前段比纯模型更平。但当：
•	N 大到 prefetcher 跟不上（stream 数超过 prefetcher 容量，通常 ~16 streams）；
•	或者跨 4KB page 边界（很多 prefetcher 不跨页）；
•	或者 working-set 大到 prefetcher 把有用数据挤出 cache（prefetch 污染）；
prefetcher 就会突然失效甚至变成负贡献。这会让曲线在某个 N 上出现一个比纯 cache 模型更陡的台阶。
7. "等最慢 reader" 的尾延迟随 N 放大
writer 等的是 max⁡kTk\max_k T_kmaxkTk。N 越大，每个 reader 自己也要遍历 N 条 line 去校验/ack。每条 line 都有一个小概率撞上 LLC miss / TLB miss / 中断等抖动事件。
设单条 line 撞上慢路径的概率为 ppp，那么 reader 处理 N 条 line 中至少一条慢的概率是 1−(1−p)N1 - (1-p)^N1−(1−p)N，对小 ppp 近似 NpNpNp。也就是说：
Pr⁡[reader 命中尾延迟]  ≈  Np\Pr[\text{reader 命中尾延迟}] \;\approx\; NpPr[reader 命中尾延迟]≈Np
writer 等的是 K 个 reader 中最慢的，最终命中尾延迟的概率约为 1−(1−Np)K≈NKp1 - (1 - Np)^K \approx NKp1−(1−Np)K≈NKp。N 和 K 在尾部是相乘关系，这解释了为什么"大 N + 多核"会比单独大 N 或单独多核糟糕得多——尾延迟项是双线性放大的。
8. 把所有项合起来
writer 延迟可以粗略写成：
Twriter(N,K)  ≈  N⋅[tcache(N)+K⋅tsnoop(N,K)]  +  Ttail(N,K)T_{\text{writer}}(N, K) \;\approx\; N \cdot \big[t_{\text{cache}}(N) + K \cdot t_{\text{snoop}}(N, K)\big] \;+\; T_{\text{tail}}(N, K)Twriter(N,K)≈N⋅[tcache(N)+K⋅tsnoop(N,K)]+Ttail(N,K)
•	tcache(N)t_{\text{cache}}(N)tcache(N)：随 N 阶梯上升（L1/L2/LLC/DRAM）
•	tsnoop(N,K)t_{\text{snoop}}(N, K)tsnoop(N,K)：在 snoop filter cliff 之后阶梯上升
•	Ttail(N,K)T_{\text{tail}}(N, K)Ttail(N,K)：极值统计项，关于 N⋅KN \cdot KN⋅K 增长
所以任意一项都让曲线随 N 上升，再叠加上 cliff 结构，就是你看到的那种"阶梯上升 + 偶有陡升"的形状。
9. 怎么判断你看到的是哪种 cliff？
实战里区分这几种 cliff 的小技巧：
•	把 N 换成字节横轴 N⋅64BN \cdot 64\text{B}N⋅64B，cliff 落在 L1/L2/LLC 容量上 → cache cliff。
•	改用 hugepage 后 cliff 消失 / 显著右移 → TLB cliff。
•	改变 reader 数 K，cliff 位置左右移动（K↑则左移）→ snoop filter cliff。
•	关闭 prefetcher（MSR 0x1A4）后曲线整体上抬但 cliff 形态不变 → 之前低位被 prefetcher 压平了，现在看到的是"裸"曲线。
•	单线程跑（K=0，没有 reader），cliff 仍在但没有 snoop 项 → 纯 cache/TLB cliff。
把这几组对照画在一起，你这条 benchmark 的曲线就被完全"解构"了：哪些是 N 本身贡献的、哪些是 cache 容量贡献的、哪些是 coherence/snoop filter 贡献的，一目了然。
________________________________________
一句话总结：N 增大 = 写更多 line + working-set 跨越更多容量边界 + coherence 流量增多 + 尾延迟概率累积，这四件事任何一件都让 TwriterT_{\text{writer}}Twriter 上升，叠加在一起就是你看到的"随 N 单调上升、带阶梯"的曲线。这条曲线本身就是测试想暴露的东西。


Thinking...
用户在问为什么在 snoop filter benchmark 测试中，使用的 core 数量增加会导致测试耗时变长。我需要从硬件层面来解释这个现象背后的原因。
为什么 Core 数越多，writer 的延迟就越长？
这其实是该测试最核心的物理现象之一，不是 bug，而是 cache coherence 协议的本质开销。可以从下面几个层次来理解。
1. Writer 测的是 fan-out，本质是"等所有 reader 都看到"
回顾测试逻辑：
t_send = rdtsc();
for (i = 0; i < N; i++) lines[i].version++;   // 写 N 条 cache line
wait_until(all_readers_acked);                 // ★ 等待所有 reader ack
t_done = rdtsc();
writer 测的延迟是 t_done - t_send，它包含了等待所有 reader ack 的时间。所以这是一个 1 → K fan-out 操作，其延迟随 reader 数 KKK 增长是天然的，不可避免。
具体来看，延迟由三部分组成：
Twriter  =  Tinvalidate(K)  +  Tack-wait(K)  +  Trfo-refill(K)T_{\text{writer}} \;=\; T_{\text{invalidate}}(K) \;+\; T_{\text{ack-wait}}(K) \;+\; T_{\text{rfo-refill}}(K)Twriter=Tinvalidate(K)+Tack-wait(K)+Trfo-refill(K)
•	TinvalidateT_{\text{invalidate}}Tinvalidate：writer 写 line 时，coherence 协议（MESI/MOESI）必须把所有持有 Shared 副本的 reader 上的副本 invalidate。持有副本的核越多，需要发出/收集的 snoop 消息就越多。
•	Tack-waitT_{\text{ack-wait}}Tack-wait：writer 在 spin 等所有 reader 把 ack 写进共享 slot；这是一个"取最慢者"操作，延迟 ≈ max⁡kTk\max_k T_kmaxkTk，K 越大、最慢那个 reader 的尾延迟越显著（极值统计）。
•	Trfo-refillT_{\text{rfo-refill}}Trfo-refill：reader 发回 ack 时把自己的 ack line 改成 M 状态，writer 下一轮读 ack 又要把它变回自己 own，这又是一次 RFO + invalidate，K 个 reader 就有 K 条 ack line。
2. Coherence 协议本身就不是 O(1)
不同微架构的 snoop 实现，对 fan-out 的 scaling 曲线不同：
实现类型	典型架构	fan-out 复杂度	说明
Broadcast snoop	早期 SMP / 小核数 Ryzen 内 CCX	O(K)O(K)O(K) 带宽，O(1)O(1)O(1) 延迟（理想）	总线广播，但带宽受限
Directory / snoop filter	Intel mesh、AMD 跨 CCD、ARM SLC	O(log⁡K)O(\log K)O(logK) 到 O(K)O(K)O(K)	目录查表后定向 invalidate
Hierarchical	NUMA / multi-socket	O(K)+TxsocketO(K) + T_{\text{xsocket}}O(K)+Txsocket
多了跨 die / 跨 socket 的 hop
即便最好的实现，writer 也至少要收集 K 个 ack，这就是一个 reduction 操作，理论下界就是 O(log⁡K)O(\log K)O(logK)（甚至在硬件层面通常退化为 O(K)O(K)O(K)，因为 ack 是逐个写回到 writer 的 cache line）。
3. "取最慢者"效应（tail latency amplification）
writer 在等的是 最后一个 reader 的 ack，所以 writer 看到的延迟是：
Twriter≈max⁡k=1..KTreaderkT_{\text{writer}} \approx \max_{k=1..K} T_{\text{reader}_k}Twriter≈k=1..KmaxTreaderk
即使每个 reader 的延迟分布完全一样、均值很小，最大值的期望会随 K 增大而增大。一个常见近似：若单个 reader 延迟服从指数分布 Exp(λ)\text{Exp}(\lambda)Exp(λ)，则：
E[max⁡k=1..KTk]  =  1λ∑i=1K1i  ≈  ln⁡K+γλ\mathbb{E}[\max_{k=1..K} T_k] \;=\; \frac{1}{\lambda}\sum_{i=1}^{K}\frac{1}{i} \;\approx\; \frac{\ln K + \gamma}{\lambda}E[k=1..KmaxTk]=λ1i=1∑Ki1≈λlnK+γ
也就是说，即使硬件没有任何 contention，仅仅是统计极值效应，writer 延迟也会随 ln⁡K\ln KlnK 增长。如果分布有重尾（实际上 cache 操作几乎一定有重尾，比如 C-state 唤醒、TLB miss、SMI），增长比 ln⁡K\ln KlnK 还快。
4. Mesh / interconnect 上的拥塞
K 越大，单位时间内 mesh / fabric 上跑的 coherence 消息就越多，路径上会出现：
•	snoop 请求与 ack 在 ring/mesh 上排队，CHA / HA 队列拥塞；
•	同一时刻多个 reader 命中同一 LLC slice，slice 串行化；
•	写回的 ack line 之间互相 false sharing（如果 ack slot 没对齐到独立 cache line，问题更严重）；
•	跨 die / 跨 socket 链路（IF / UPI / CCIX）带宽受限，多 reader 同时 ack 时排队。
这些都会让"单条 invalidate 路径的延迟"也变长，是叠加在前面 log⁡K\log KlogK 之上的额外项。
5. Snoop filter 压力随 K 放大
这一点容易被忽略：snoop filter 跟踪的是 (cache line,owner-set)(\text{cache line}, \text{owner-set})(cache line,owner-set)。reader 数 K 增大时，每条 line 的 owner-set 更大，filter 中的 presence vector 更宽。一旦超过 filter 容量，就触发 back-invalidation。所以：
K 越大，cliff 越早出现（在更小的 working-set 处）；K 大时既有"水平段更高"又有"cliff 更靠左"两个现象同时出现。
这就是为什么不同 reader 数下重测，曲线整体上抬 + 拐点左移，而不是简单的平移。
6. 与 reader 自身延迟的对比
如果你看 reader 的曲线，你会发现：reader 的 p50 几乎不随 K 增长（或增长很慢），因为单个 reader 看到的就是"writer 写的那一条 line 何时到我"——这只取决于 writer→reader 的单跳延迟，与其他 reader 数量基本无关（除非 mesh 拥塞影响传输）。
而 writer 延迟随 K 显著增长，这正好印证了上面所说的 fan-out + 取最慢者 + 拥塞 三重叠加。
7. 实践建议
如果你的目的是对比不同平台而不是绝对值：
•	固定 reader 数再做跨平台对比，否则不同平台的核数差异会主导差异，盖过 snoop filter 行为本身。比如统一取 readers = min(num_cores) - 1，或都取 8、16、32 三档分别比。
•	在报告中用归一化曲线（脚本里已有 4.5 节）抵消 K 带来的"水平基线上抬"，专注对比 cliff 形态。
•	如果想进一步分离 fan-out 开销与 snoop filter 开销，可以补一组 N=1（单条 line）的扫描，作为纯 fan-out 基线；用 writer 总延迟减去这一基线，剩下的近似就是 snoop filter / interconnect 容量效应。
•	关闭 SMT、固定频率、isolcpus + nohz_full 隔离测试核，能显著降低尾延迟，让 O(log⁡K)O(\log K)O(logK) 的统计极值项不至于被噪声放大成 O(K)O(K)O(K)。
简单总结：writer 测的是"广播 + 全员 ack"的同步原语开销，它在物理上就是一个随核数增长的量——这恰恰是这个 benchmark 想测的东西，而不是测量误差。


