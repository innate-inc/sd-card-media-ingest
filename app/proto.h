/* Serial line protocol parser (host -> device).
 *
 * One command per newline-terminated line. sscanf-parseable, no allocator.
 * Any received line also counts as activity and pulses the heartbeat.
 *
 *   hb                                   heartbeat / keepalive
 *   clear                                remove all slots
 *   count <n>                            truncate the slot list to n
 *   bg <rrggbb>                          background / "empty space" colour
 *   numbers <0|1>                        show per-segment numbers
 *   slot <i> <size_mb> <eta_s> <status> \
 *        <p0> <c0> <p1> <c1> <p2> <c2> <p3> <c3> <label...>
 *                                        define/update slot i
 *
 *   <i>        0-based slot index
 *   <size_mb>  total size MB, -1 = unknown
 *   <eta_s>    seconds to done, -1 = unknown
 *   <status>   idle|active|done|error|paused|pending
 *   <pN> <cN>  segment N: permille (0..1000) and colour (hex rrggbb);
 *              permille 0 = unused segment. Segments stack; leftover shows bg.
 *   <label>    rest of the line
 */
#ifndef INGEST_PROTO_H
#define INGEST_PROTO_H

#include "model.h"

typedef struct {
    int changed;
    int heartbeat;
} proto_result_t;

proto_result_t proto_handle_line(model_t *m, const char *line);

#endif /* INGEST_PROTO_H */
