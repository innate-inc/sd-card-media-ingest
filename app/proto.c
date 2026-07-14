#include "proto.h"

#include <string.h>
#include <stdio.h>

/* Copy a trailing free-text field: bounded, NUL-terminated, truncated at the
 * first CR/LF. Shared by the label / legend-text / detail-path parsers. */
static void copy_field(char *dst, const char *src, int cap) {
    strncpy(dst, src, cap - 1);
    dst[cap - 1] = '\0';
    for (char *q = dst; *q; q++)
        if (*q == '\r' || *q == '\n') { *q = '\0'; break; }
}

static slot_status_t parse_status(const char *s) {
    if (!strcmp(s, "active")) return ST_ACTIVE;
    if (!strcmp(s, "done")) return ST_DONE;
    if (!strcmp(s, "error")) return ST_ERROR;
    if (!strcmp(s, "paused")) return ST_PAUSED;
    if (!strcmp(s, "pending")) return ST_PENDING;
    return ST_IDLE;
}

proto_result_t proto_handle_line(model_t *m, const char *raw) {
    proto_result_t r = {0, 0};

    /* Dispatch on a trimmed copy: a stray trailing '\r' (CRLF host) or spaces
     * must not turn an exact-match command like `clear` / `legend clear` into
     * an unrecognised line. Leading blanks are skipped too. */
    char buf[256];
    while (*raw == ' ' || *raw == '\t') raw++;
    size_t len = strlen(raw);
    while (len > 0 && (raw[len - 1] == '\r' || raw[len - 1] == '\n' ||
                       raw[len - 1] == ' ' || raw[len - 1] == '\t')) len--;
    if (len >= sizeof buf) len = sizeof buf - 1;
    memcpy(buf, raw, len);
    buf[len] = '\0';
    const char *line = buf;

    if (*line == '\0' || *line == '#') return r;
    r.heartbeat = 1;

    if (!strncmp(line, "hb", 2) && (line[2] == '\0' || line[2] == ' ')) return r;

    if (!strcmp(line, "clear")) {
        m->count = 0;
        /* drop per-slot detail too, so a stale path/UUID can't outlive its card */
        for (int i = 0; i < MAX_SLOTS; i++) m->slots[i].detail[0] = '\0';
        r.changed = 1;
        return r;
    }

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

    if (!strcmp(line, "legend clear")) { m->nlegend = 0; r.changed = 1; return r; }
    {
        unsigned lc; int lconsumed = 0;
        if (sscanf(line, "legend %x %n", &lc, &lconsumed) == 1 && lconsumed > 0
            && line[lconsumed] != '\0') {          /* require non-empty text */
            if (m->nlegend < MAX_LEGEND) {
                legend_t *e = &m->legend[m->nlegend];
                e->color = lc & 0xFFFFFF;
                copy_field(e->text, line + lconsumed, MAX_LABEL);
                m->nlegend++;
            }
            r.changed = 1;
            return r;
        }
    }

    {
        int pidx, pconsumed = 0;
        if (sscanf(line, "path %d %n", &pidx, &pconsumed) == 1 && pconsumed > 0
            && pidx >= 0 && pidx < MAX_SLOTS) {
            copy_field(m->slots[pidx].detail, line + pconsumed, MAX_DETAIL);
            r.changed = 1;
            return r;
        }
    }

    int idx, size, eta, kbps, consumed = 0;
    int p[MAX_SEGS];
    unsigned c[MAX_SEGS];
    char status[16];
    if (sscanf(line, "slot %d %d %d %d %15s %d %x %d %x %d %x %d %x %n",
               &idx, &size, &eta, &kbps, status,
               &p[0], &c[0], &p[1], &c[1], &p[2], &c[2], &p[3], &c[3],
               &consumed) >= 13) {
        if (idx < 0 || idx >= MAX_SLOTS) return r;
        slot_t *s = &m->slots[idx];
        s->size_mb = size;
        s->eta_s = eta;
        s->kbps = kbps;
        s->status = parse_status(status);
        for (int k = 0; k < MAX_SEGS; k++) {
            int pm = p[k];
            if (pm < 0) pm = 0;
            if (pm > 1000) pm = 1000;
            s->segs[k].permille = (uint16_t)pm;
            s->segs[k].color = c[k] & 0xFFFFFF;
        }
        copy_field(s->label, line + consumed, MAX_LABEL);
        if (idx >= m->count) m->count = idx + 1;
        r.changed = 1;
        return r;
    }

    return r;
}
