/*
 * snoop_bench.c
 *
 * Core A (writer) writes N cache lines per iteration; all other cores (readers) read them
 * and ack. Writer measures end-to-end fan-out latency. Each reader measures its own
 * write-to-observe latency. Sweeping N stresses the snoop filter.
 *
 * Build: gcc -O2 -pthread -o snoop_bench snoop_bench.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include <stdatomic.h>
#include <errno.h>
#include <getopt.h>
#include <sys/mman.h>

#define CL 64
#define ALIGNED __attribute__((aligned(CL)))

static inline uint64_t rdtscp_now(void) {
    unsigned aux;
    uint32_t lo, hi;
    __asm__ volatile("rdtscp" : "=a"(lo), "=d"(hi), "=c"(aux) :: "memory");
    return ((uint64_t)hi << 32) | lo;
}
static inline void cpu_relax(void) { __asm__ volatile("pause" ::: "memory"); }

typedef struct { _Atomic uint64_t seq; _Atomic uint64_t send_tsc; char pad[CL-16]; } ALIGNED slot_t;
typedef struct { _Atomic uint64_t v; char pad[CL-8]; } ALIGNED u64_cl_t;

typedef struct {
    int role;          /* 0=writer 1=reader */
    int my_idx;        /* reader index */
    int core_id;
    int num_readers;
    int iterations;
    int warmup;
    int num_lines;
    slot_t   *slots;
    u64_cl_t *acks;     /* per-reader */
    u64_cl_t *readys;   /* per-reader */
    _Atomic int *go;
    uint64_t *latencies; /* size = iterations */
} targ_t;

static void pin_to_core(int core) {
    cpu_set_t set; CPU_ZERO(&set); CPU_SET(core, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) { perror("setaffinity"); exit(1); }
}

static void *writer_fn(void *arg) {
    targ_t *a = arg;
    pin_to_core(a->core_id);
    while (atomic_load(a->go) == 0) cpu_relax();

    int N = a->num_lines, R = a->num_readers;
    int tot = a->warmup + a->iterations;

    for (int it = 1; it <= tot; it++) {
        /* wait readers ready */
        for (int r = 0; r < R; r++)
            while (atomic_load_explicit(&a->readys[r].v, memory_order_acquire) < (uint64_t)it) cpu_relax();

        uint64_t t_send = rdtscp_now();
        /* Write payload then seq with release order, for every line */
        for (int i = 0; i < N; i++) {
            atomic_store_explicit(&a->slots[i].send_tsc, t_send, memory_order_relaxed);
            atomic_store_explicit(&a->slots[i].seq, (uint64_t)it, memory_order_release);
        }

        /* wait all acks */
        for (int r = 0; r < R; r++)
            while (atomic_load_explicit(&a->acks[r].v, memory_order_acquire) < (uint64_t)it) cpu_relax();

        uint64_t t_end = rdtscp_now();
        if (it > a->warmup) a->latencies[it - a->warmup - 1] = t_end - t_send;
    }
    return NULL;
}

static void *reader_fn(void *arg) {
    targ_t *a = arg;
    pin_to_core(a->core_id);
    while (atomic_load(a->go) == 0) cpu_relax();

    int N = a->num_lines;
    int tot = a->warmup + a->iterations;
    int idx = a->my_idx;

    for (int it = 1; it <= tot; it++) {
        atomic_store_explicit(&a->readys[idx].v, (uint64_t)it, memory_order_release);

        /* wait until last line updated; read all to force snoop traffic on every line */
        for (int i = 0; i < N; i++)
            while (atomic_load_explicit(&a->slots[i].seq, memory_order_acquire) < (uint64_t)it) cpu_relax();

        uint64_t t_recv = rdtscp_now();
        uint64_t t_send = atomic_load_explicit(&a->slots[N-1].send_tsc, memory_order_relaxed);
        if (it > a->warmup) a->latencies[it - a->warmup - 1] = t_recv - t_send;

        atomic_store_explicit(&a->acks[idx].v, (uint64_t)it, memory_order_release);
    }
    return NULL;
}

static void *xalloc(size_t sz) {
    void *p = aligned_alloc(CL, sz);
    if (!p) { perror("alloc"); exit(1); }
    memset(p, 0, sz);
    return p;
}

static int cmp_u64(const void *a, const void *b) {
    uint64_t x = *(const uint64_t*)a, y = *(const uint64_t*)b;
    return (x>y)-(x<y);
}

static void stats(uint64_t *v, int n, uint64_t *mn, uint64_t *p50, uint64_t *p95, uint64_t *p99, uint64_t *mx, double *avg) {
    qsort(v, n, sizeof(uint64_t), cmp_u64);
    *mn = v[0]; *mx = v[n-1];
    *p50 = v[n/2]; *p95 = v[(int)(n*0.95)]; *p99 = v[(int)(n*0.99)];
    double s = 0; for (int i=0;i<n;i++) s += v[i]; *avg = s/n;
}

int main(int argc, char **argv) {
    int writer_core = 0;
    int reader_cores[256]; int nrd = 0;
    int iters = 10000, warmup = 1000, num_lines = 1;
    const char *out_csv = NULL, *tag = "run";

    static struct option opts[] = {
        {"writer", required_argument, 0, 'w'},
        {"readers", required_argument, 0, 'r'},
        {"iters", required_argument, 0, 'i'},
        {"warmup", required_argument, 0, 'u'},
        {"lines", required_argument, 0, 'l'},
        {"csv", required_argument, 0, 'c'},
        {"tag", required_argument, 0, 't'},
        {0,0,0,0}
    };
    int o;
    while ((o = getopt_long(argc, argv, "w:r:i:u:l:c:t:", opts, NULL)) != -1) {
        switch (o) {
            case 'w': writer_core = atoi(optarg); break;
            case 'r': {
                char *s = strdup(optarg), *tok = strtok(s, ",");
                while (tok) { reader_cores[nrd++] = atoi(tok); tok = strtok(NULL, ","); }
                free(s); break;
            }
            case 'i': iters = atoi(optarg); break;
            case 'u': warmup = atoi(optarg); break;
            case 'l': num_lines = atoi(optarg); break;
            case 'c': out_csv = optarg; break;
            case 't': tag = optarg; break;
        }
    }
    if (nrd == 0) { fprintf(stderr,"need --readers c1,c2,...\n"); return 1; }

    slot_t   *slots  = xalloc(sizeof(slot_t)   * num_lines);
    u64_cl_t *acks   = xalloc(sizeof(u64_cl_t) * nrd);
    u64_cl_t *readys = xalloc(sizeof(u64_cl_t) * nrd);
    _Atomic int go = 0;

    int total = nrd + 1;
    pthread_t *th = calloc(total, sizeof(pthread_t));
    targ_t    *ta = calloc(total, sizeof(targ_t));
    uint64_t **lat = calloc(total, sizeof(uint64_t*));
    for (int i = 0; i < total; i++) lat[i] = calloc(iters, sizeof(uint64_t));

    /* writer */
    ta[0] = (targ_t){.role=0,.my_idx=0,.core_id=writer_core,.num_readers=nrd,
                     .iterations=iters,.warmup=warmup,.num_lines=num_lines,
                     .slots=slots,.acks=acks,.readys=readys,.go=&go,.latencies=lat[0]};
    pthread_create(&th[0], NULL, writer_fn, &ta[0]);
    /* readers */
    for (int r = 0; r < nrd; r++) {
        ta[r+1] = (targ_t){.role=1,.my_idx=r,.core_id=reader_cores[r],.num_readers=nrd,
                           .iterations=iters,.warmup=warmup,.num_lines=num_lines,
                           .slots=slots,.acks=acks,.readys=readys,.go=&go,.latencies=lat[r+1]};
        pthread_create(&th[r+1], NULL, reader_fn, &ta[r+1]);
    }

    sleep(1);
    atomic_store(&go, 1);
    for (int i = 0; i < total; i++) pthread_join(th[i], NULL);

    /* output */
    FILE *f = stdout;
    if (out_csv) {
        int exists = (access(out_csv,F_OK)==0);
        f = fopen(out_csv, "a");
        if (!f) { perror("fopen"); return 1; }
        if (!exists) fprintf(f, "tag,role,core,readers,lines,iters,min,p50,p95,p99,max,avg\n");
    } else {
        fprintf(f, "tag,role,core,readers,lines,iters,min,p50,p95,p99,max,avg\n");
    }

    for (int i = 0; i < total; i++) {
        uint64_t mn,p50,p95,p99,mx; double avg;
        stats(lat[i], iters, &mn,&p50,&p95,&p99,&mx,&avg);
        fprintf(f, "%s,%s,%d,%d,%d,%d,%lu,%lu,%lu,%lu,%lu,%.1f\n",
                tag, i==0?"writer":"reader", ta[i].core_id, nrd, num_lines, iters,
                mn,p50,p95,p99,mx,avg);
    }
    if (out_csv) fclose(f);

    /* also dump per-iteration raw if asked via env */
    const char *raw = getenv("RAW_DUMP");
    if (raw) {
        FILE *rf = fopen(raw, "w");
        fprintf(rf, "tag,role,core,iter,latency_cycles\n");
        for (int i = 0; i < total; i++)
            for (int j = 0; j < iters; j++)
                fprintf(rf, "%s,%s,%d,%d,%lu\n",
                        tag, i==0?"writer":"reader", ta[i].core_id, j, lat[i][j]);
        fclose(rf);
    }
    return 0;
}
