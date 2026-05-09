Snoop filter 的性能特征只在以下条件出现：
1.	工作集大小（cache line 数）扫描：从远小于 snoop filter 容量扫到远大于。Intel Skylake X / Cascade Lake 上 snoop filter 的有效 entry 数 ≈ 每个 LLC slice 的若干倍 LLC ways（典型 ~1.5× 2× LLC 行数）。当 working set 越过这个阈值，会出现明显的"性能悬崖"。
2.	多核共享同一组 cache line，让这些行在 snoop filter 中被多核 valid bit 标记。
3.	流式访问强制 snoop filter 频繁查表/插入/驱逐。

数据集越大latency越大，core数量越多latency越大
