#include "proto.h"

#include <string.h>
#include <stdio.h>

static slot_status_t parse_status(const char *s) {
    if (!strcmp(s, "active")) return ST_ACTIVE;
    if (!strcmp(s, "done")) return ST_DONE;
    if (!strcmp(s, "error")) return ST_ERROR;
    if (!strcmp(s, "paused")) return ST_PAUSED;
    if (!strcmp(s, "pending")) return ST_PENDING;
    return ST_IDLE;
}

proto_result_t proto_handle_line(model_t *m, const char *line) {
    proto_result_t r = {0, 0};

    while (*line == ' ' || *line == '\t') line++;
    if (*line == '\0' || *line == '#') return r;
    r.heartbeat = 1;

    if (!strncmp(line, "hb", 2) && (line[2] == '\0' || line[2] == ' ')) return r;

    if (!strcmp(line, "clear")) { m->count = 0; r.changed = 1; return r; }

    int n;
    unsigned hex;
    if (sscanf(line, "count %d", &n) == 1) {
        if (n < 0) n = 0;
        if (n > MAX_SLOTS) n = MAX_SLOTS;
        m->count = n; r.changed = 1; return r;
    }
    if (sscanf(line, "bg %x", &hex) == 1) {
        m->empty_color = hex & 0xFFFFFF; r.changed = 1; return r;
    }
    if (sscanf(line, "numbers %d", &n) == 1) {
        m->show_numbers = n ? 1 : 0; r.changed = 1; return r;
    }

    int idx, size, eta, consumed = 0;
    int p[MAX_SEGS];
    unsigned c[MAX_SEGS];
    char status[16];
    if (sscanf(line, "slot %d %d %d %15s %d %x %d %x %d %x %d %x %n",
               &idx, &size, &eta, status,
               &p[0], &c[0], &p[1], &c[1], &p[2], &c[2], &p[3], &c[3],
               &consumed) >= 12) {
        if (idx < 0 || idx >= MAX_SLOTS) return r;
        slot_t *s = &m->slots[idx];
        s->size_mb = size;
        s->eta_s = eta;
        s->status = parse_status(status);
        s->nsegs = MAX_SEGS;
        for (int k = 0; k < MAX_SEGS; k++) {
            int pm = p[k];
            if (pm < 0) pm = 0;
            if (pm > 1000) pm = 1000;
            s->segs[k].permille = (uint16_t)pm;
            s->segs[k].color = c[k] & 0xFFFFFF;
        }
        const char *label = line + consumed;
        strncpy(s->label, label, MAX_LABEL - 1);
        s->label[MAX_LABEL - 1] = '\0';
        for (char *q = s->label; *q; q++) {
            if (*q == '\r' || *q == '\n') { *q = '\0'; break; }
        }
        if (idx >= m->count) m->count = idx + 1;
        r.changed = 1;
        return r;
    }

    return r;
}
