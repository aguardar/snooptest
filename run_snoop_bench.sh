#!/usr/bin/env bash
# Snoop filter / coherence sweep benchmark driver.
#
# Default: writer = core 0, readers = all other online logical CPUs.
# Override:
#   ./run_snoop_bench.sh [writer_core] [reader_cores_csv] [out_csv]
#   ITERS=, WARMUP=, LINES_LIST= 等环境变量也可覆盖。
#
# Examples:
#   ./run_snoop_bench.sh                       # writer=0, readers=all others
#   ./run_snoop_bench.sh 0                     # 同上
#   ./run_snoop_bench.sh 0 1,2,3 my.csv        # 指定 readers
#   LINES_LIST="1 64 4096" ./run_snoop_bench.sh
set -euo pipefail

############################################
# 1. 解析在线 CPU 列表
############################################
# /sys/devices/system/cpu/online 形如: "0-7" 或 "0,2-5,8"
parse_cpu_list() {
    local s="$1" out=() part a b
    IFS=',' read -ra parts <<< "$s"
    for part in "${parts[@]}"; do
        if [[ "$part" == *-* ]]; then
            a="${part%-*}"; b="${part#*-}"
            for ((i=a; i<=b; i++)); do out+=("$i"); done
        else
            out+=("$part")
        fi
    done
    echo "${out[@]}"
}

ONLINE_RAW=$(cat /sys/devices/system/cpu/online)
ALL_CPUS=( $(parse_cpu_list "$ONLINE_RAW") )

############################################
# 2. 处理参数 / 默认值
############################################
WRITER="${1:-0}"

if [[ $# -ge 2 && -n "${2:-}" ]]; then
    READERS="$2"
else
    # 默认：除 writer 外的所有在线核
    READERS_ARR=()
    for c in "${ALL_CPUS[@]}"; do
        [[ "$c" == "$WRITER" ]] && continue
        READERS_ARR+=("$c")
    done
    if [[ ${#READERS_ARR[@]} -eq 0 ]]; then
        echo "ERROR: 没有可用的 reader 核（系统只有 1 个在线 CPU 或 writer 选错了）" >&2
        exit 1
    fi
    # 用逗号拼接
    READERS=$(IFS=','; echo "${READERS_ARR[*]}")
fi

OUT="${3:-results.csv}"

ITERS="${ITERS:-20000}"
WARMUP="${WARMUP:-2000}"

# 工作集扫描点（cache line 数）。可通过环境变量覆盖。
# 1 行即原始 ping-pong 场景；上界尽量超过 snoop filter 容量。
LINES_LIST="${LINES_LIST:-1 2 4 8 16 32 64 128 256 512 1024 2048 4096 8192 16384 32768 65536 131072 262144 524288}"

############################################
# 3. 健壮性提示
############################################
NREAD=$(awk -F, '{print NF}' <<< "$READERS")
echo "================================================================"
echo " writer core   : $WRITER"
echo " reader cores  : $READERS  (count=$NREAD)"
echo " online CPUs   : ${ALL_CPUS[*]}  (total=${#ALL_CPUS[@]})"
echo " iterations    : $ITERS  (warmup=$WARMUP)"
echo " lines sweep   : $LINES_LIST"
echo " output csv    : $OUT"
echo "================================================================"

# 检查 writer 是否在线
in_online=0
for c in "${ALL_CPUS[@]}"; do [[ "$c" == "$WRITER" ]] && in_online=1; done
if [[ $in_online -eq 0 ]]; then
    echo "ERROR: writer core $WRITER 不在在线 CPU 列表中" >&2
    exit 1
fi

# 检查二进制
if [[ ! -x ./snoop_bench ]]; then
    echo "ERROR: ./snoop_bench 不存在或不可执行，请先 'gcc -O2 -pthread -o snoop_bench snoop_bench.c'" >&2
    exit 1
fi

############################################
# 4. 性能稳态设置（best-effort，可选）
############################################
if command -v cpupower >/dev/null 2>&1; then
    sudo cpupower frequency-set -g performance >/dev/null 2>&1 || true
fi
if [[ -w /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
    echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo >/dev/null 2>&1 || true
fi

############################################
# 5. 执行扫描
############################################
rm -f "$OUT"

for L in $LINES_LIST; do
    TAG="lines=${L}"
    echo "--- running $TAG ---"
    ./snoop_bench \
        --writer  "$WRITER" \
        --readers "$READERS" \
        --iters   "$ITERS" \
        --warmup  "$WARMUP" \
        --lines   "$L" \
        --csv     "$OUT" \
        --tag     "$TAG"
done

echo "================================================================"
echo "Done. Aggregated results in: $OUT"
echo "Rows: $(wc -l < "$OUT")"