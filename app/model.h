/* Shared data model for the ingest display (device + simulator).
 *
 * The firmware is a *generic* renderer: each slot's bar is a stack of up to
 * MAX_SEGS segments, each just a (portion, colour) pair. The server decides
 * what the segments mean (uploaded / copied / uncopied / ...), their colours,
 * and whether to show numbers. Leftover space shows the configurable
 * background ("empty") colour.
 */
#ifndef INGEST_MODEL_H
#define INGEST_MODEL_H

#include <stdint.h>

#define MAX_SLOTS 32
#define MAX_LABEL 24
#define MAX_SEGS  4

typedef enum {
    ST_IDLE = 0,
    ST_ACTIVE,
    ST_DONE,
    ST_ERROR,
    ST_PAUSED,
    ST_PENDING,   /* verified, awaiting human confirmation to wipe */
} slot_status_t;

typedef struct {
    uint16_t permille;   /* portion of the whole bar, 0..1000 */
    uint32_t color;      /* 0xRRGGBB */
} segment_t;

typedef struct {
    char label[MAX_LABEL];
    int nsegs;
    segment_t segs[MAX_SEGS];
    int32_t size_mb;     /* total size in MB, -1 = unknown (for numbers) */
    int32_t eta_s;       /* seconds to completion, -1 = unknown */
    slot_status_t status;
} slot_t;

typedef struct {
    slot_t slots[MAX_SLOTS];
    int count;
    uint32_t empty_color;   /* configurable background / "empty space" colour */
    int show_numbers;       /* server toggle: draw per-segment numbers */
} model_t;

static inline void model_init(model_t *m) {
    m->count = 0;
    m->empty_color = 0x202020;
    m->show_numbers = 0;
}

#endif /* INGEST_MODEL_H */
