/* Unit tests for the serial line protocol parser.
 *
 * "Mocking" here = feeding fake serial lines into proto_handle_line() and
 * asserting the resulting model, with no device, LVGL, or real serial port.
 * Pure logic from app/proto.c + app/model.h, compiled for the host.
 */
#include "proto.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static model_t m;

int main(void) {
    model_init(&m);
    assert(m.count == 0);
    assert(m.empty_color == 0x202020);

    /* a slot line: parsed fields + auto-extends count */
    proto_result_t r = proto_handle_line(
        &m, "slot 0 238000 900 42000 active 300 22c35e 200 0072b2 250 e69f00 0 0 SANDISK64");
    assert(r.changed && r.heartbeat);
    assert(m.count == 1);
    assert(m.slots[0].status == ST_ACTIVE);
    assert(m.slots[0].size_mb == 238000);
    assert(m.slots[0].eta_s == 900);
    assert(m.slots[0].kbps == 42000);
    assert(m.slots[0].segs[0].permille == 300);
    assert(m.slots[0].segs[0].color == 0x22c35e);
    assert(m.slots[0].segs[2].permille == 250);
    assert(m.slots[0].segs[2].color == 0xe69f00);
    assert(strcmp(m.slots[0].label, "SANDISK64") == 0);

    /* index 3 auto-extends count to 4; negative eta/kbps preserved */
    proto_handle_line(&m, "slot 3 64000 -1 -1 error 100 22c35e 0 0 0 0 0 0 USBSTICK");
    assert(m.count == 4);
    assert(m.slots[3].status == ST_ERROR);
    assert(m.slots[3].eta_s == -1);
    assert(m.slots[3].kbps == -1);

    /* hb = heartbeat only, no model change */
    r = proto_handle_line(&m, "hb");
    assert(r.heartbeat && !r.changed);

    /* comments and blank lines: ignored entirely (no heartbeat) */
    r = proto_handle_line(&m, "# a comment");
    assert(!r.heartbeat && !r.changed);
    r = proto_handle_line(&m, "");
    assert(!r.heartbeat && !r.changed);
    r = proto_handle_line(&m, "   ");
    assert(!r.heartbeat && !r.changed);

    /* bg / numbers */
    r = proto_handle_line(&m, "bg 123456");
    assert(r.changed && m.empty_color == 0x123456);
    proto_handle_line(&m, "numbers 1");
    assert(m.show_numbers == 1);
    proto_handle_line(&m, "numbers 0");
    assert(m.show_numbers == 0);

    /* count truncates; clear empties */
    proto_handle_line(&m, "count 2");
    assert(m.count == 2);
    proto_handle_line(&m, "clear");
    assert(m.count == 0);

    /* permille clamps to 1000 */
    proto_handle_line(&m, "slot 0 -1 -1 -1 idle 2000 ffffff 0 0 0 0 0 0 X");
    assert(m.slots[0].segs[0].permille == 1000);

    /* over-long label truncated to MAX_LABEL-1 */
    proto_handle_line(&m,
        "slot 0 0 0 0 idle 0 0 0 0 0 0 0 0 THISLABELISWAYTOOLONGFORTHEBUFFER");
    assert(strlen(m.slots[0].label) <= (size_t)(MAX_LABEL - 1));

    /* out-of-range index ignored, count unchanged */
    int before = m.count;
    proto_handle_line(&m, "slot 99 0 0 0 idle 0 0 0 0 0 0 0 0 X");
    assert(m.count == before);

    /* path: optional per-slot detail string, spaces preserved */
    proto_handle_line(&m, "slot 0 0 0 0 idle 0 0 0 0 0 0 0 0 CARD");
    r = proto_handle_line(&m, "path 0 /mnt/ingest/8A1F-3C2D");
    assert(r.changed);
    assert(strcmp(m.slots[0].detail, "/mnt/ingest/8A1F-3C2D") == 0);

    /* legend: appends colour+text entries; text keeps spaces */
    proto_handle_line(&m, "legend clear");
    assert(m.nlegend == 0);
    r = proto_handle_line(&m, "legend 22c35e uploaded");
    assert(r.changed && m.nlegend == 1);
    assert(m.legend[0].color == 0x22c35e);
    assert(strcmp(m.legend[0].text, "uploaded") == 0);
    proto_handle_line(&m, "legend 202020 free space");
    assert(m.nlegend == 2);
    assert(strcmp(m.legend[1].text, "free space") == 0);
    proto_handle_line(&m, "legend clear");
    assert(m.nlegend == 0);

    /* junk line: heartbeat but no change */
    r = proto_handle_line(&m, "florble wizzbang");
    assert(r.heartbeat && !r.changed);

    /* CRLF tolerance: a trailing \r must not defeat exact-match commands */
    proto_handle_line(&m, "legend 22c35e uploaded");
    assert(m.nlegend == 1);
    r = proto_handle_line(&m, "legend clear\r");   /* CRLF host */
    assert(r.changed && m.nlegend == 0);           /* cleared, not a junk row */
    proto_handle_line(&m, "slot 0 0 0 0 idle 0 0 0 0 0 0 0 0 CARD\r");
    assert(strcmp(m.slots[0].label, "CARD") == 0); /* label has no stray \r */
    proto_handle_line(&m, "count 3");
    proto_handle_line(&m, "clear\r");
    assert(m.count == 0);

    /* clear also drops stale per-slot detail */
    proto_handle_line(&m, "slot 0 0 0 0 idle 0 0 0 0 0 0 0 0 CARD");
    proto_handle_line(&m, "path 0 /mnt/ingest/OLD");
    assert(m.slots[0].detail[0] != '\0');
    proto_handle_line(&m, "clear");
    assert(m.slots[0].detail[0] == '\0');

    /* legend with no text is rejected (no empty row) */
    proto_handle_line(&m, "legend clear");
    r = proto_handle_line(&m, "legend 22c35e");
    assert(m.nlegend == 0);

    /* legend is capped at MAX_LEGEND; overflow rows are dropped */
    proto_handle_line(&m, "legend clear");
    for (int i = 0; i < MAX_LEGEND + 3; i++)
        proto_handle_line(&m, "legend 010203 row");
    assert(m.nlegend == MAX_LEGEND);

    /* out-of-range path index is ignored, not written out of bounds */
    r = proto_handle_line(&m, "path 99 /mnt/ingest/NOPE");
    assert(!r.changed);

    printf("test_proto: all assertions passed\n");
    return 0;
}
