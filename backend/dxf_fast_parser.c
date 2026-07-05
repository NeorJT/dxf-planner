/*
 * dxf_fast_parser.c - v2
 *
 * Custom DXF parser optimized for rendering 2D geometry.
 * v2: Adds INSERT block expansion, HATCH support, better LWPOLYLINE
 *
 * Compiles as a Windows DLL for use with Python ctypes.
 */

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#define _stricmp strcasecmp
#include <strings.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <ctype.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ---------- Hash table for layer+color batching ---------- */
#define BATCH_TABLE_SIZE 16384
#define MAX_LAYER_NAME 256
#define MAX_NAME 64

typedef struct BatchEntry {
    char layer[MAX_LAYER_NAME];
    int color;
    float *verts;
    int vert_count;
    int vert_capacity;
    struct BatchEntry *next;
} BatchEntry;

typedef struct {
    BatchEntry *buckets[BATCH_TABLE_SIZE];
    int batch_count;
    int total_segments;
    float bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y;
} BatchTable;

/* ---------- Layer info ---------- */
typedef struct LayerInfo {
    char name[MAX_LAYER_NAME];
    int color;
    struct LayerInfo *next;
} LayerInfo;

static LayerInfo *g_layers = NULL;

/* ---------- Block registry (for INSERT expansion) ---------- */
#define MAX_BLOCKS 512
#define MAX_ENTITIES_PER_BLOCK 100000

typedef enum {
    ENT_LINE,
    ENT_ARC,
    ENT_CIRCLE,
    ENT_LWPLY,
    ENT_ELLIPSE,
    ENT_INSERT
} EntType;

typedef struct {
    int n_pts;
    float pts[8][2];  /* up to 8 points (LWPOLYLINE/SOLID) */
    int closed;
} LWPolyData;

typedef struct BlockEntity {
    EntType type;
    char layer[MAX_LAYER_NAME];
    int color;
    union {
        struct { float x1, y1, x2, y2; } line;
        struct { float cx, cy, r, sa, ea; } arc;
        struct { float cx, cy, r; } circle;
        LWPolyData poly;
        struct { float cx, cy, mx, my, ratio, start, end; } ellipse;
        struct { char name[MAX_NAME]; float x, y, xs, ys, rot; } insert;
    } d;
} BlockEntity;

typedef struct BlockDef {
    char name[MAX_NAME];
    float base_x;
    float base_y;
    int n_ents;
    BlockEntity *ents;
    int ents_cap;
    struct BlockDef *next;
} BlockDef;

static BlockDef *g_blocks = NULL;

/* ---------- Hash ---------- */
static unsigned int hash_str(const char *s) {
    unsigned int h = 5381;
    for (; *s; s++) h = ((h << 5) + h) + (unsigned char)*s;
    return h;
}

static BatchEntry* get_or_create_batch(BatchTable *table, const char *layer, int color) {
    unsigned int idx = hash_str(layer) ^ (unsigned int)color;
    idx = idx % BATCH_TABLE_SIZE;
    BatchEntry *e = table->buckets[idx];
    while (e) {
        if (e->color == color && strcmp(e->layer, layer) == 0) return e;
        e = e->next;
    }
    e = (BatchEntry*)calloc(1, sizeof(BatchEntry));
    if (!e) return NULL;
    strncpy(e->layer, layer, MAX_LAYER_NAME - 1);
    e->color = color;
    e->vert_capacity = 256;
    e->verts = (float*)malloc(e->vert_capacity * sizeof(float));
    e->next = table->buckets[idx];
    table->buckets[idx] = e;
    table->batch_count++;
    return e;
}

static void batch_append(BatchTable *table, BatchEntry *e, float x0, float y0, float x1, float y1) {
    if (e->vert_count + 2 > e->vert_capacity) {
        e->vert_capacity *= 2;
        e->verts = (float*)realloc(e->verts, e->vert_capacity * sizeof(float));
    }
    e->verts[e->vert_count++] = x0;
    e->verts[e->vert_count++] = y0;
    e->verts[e->vert_count++] = x1;
    e->verts[e->vert_count++] = y1;
    table->total_segments++;
    if (x0 < table->bbox_min_x) table->bbox_min_x = x0;
    if (y0 < table->bbox_min_y) table->bbox_min_y = y0;
    if (x1 < table->bbox_min_x) table->bbox_min_x = x1;
    if (y1 < table->bbox_min_y) table->bbox_min_y = y1;
    if (x0 > table->bbox_max_x) table->bbox_max_x = x0;
    if (y0 > table->bbox_max_y) table->bbox_max_y = y0;
    if (x1 > table->bbox_max_x) table->bbox_max_x = x1;
    if (y1 > table->bbox_max_y) table->bbox_max_y = y1;
}

static void free_batch_table(BatchTable *table) {
    for (int i = 0; i < BATCH_TABLE_SIZE; i++) {
        BatchEntry *e = table->buckets[i];
        while (e) {
            BatchEntry *next = e->next;
            free(e->verts);
            free(e);
            e = next;
        }
    }
}

static void register_layer(const char *name, int color) {
    for (LayerInfo *l = g_layers; l; l = l->next) {
        if (strcmp(l->name, name) == 0) {
            if (color >= 0) l->color = color;
            return;
        }
    }
    LayerInfo *l = (LayerInfo*)calloc(1, sizeof(LayerInfo));
    strncpy(l->name, name, MAX_LAYER_NAME - 1);
    l->color = color;
    l->next = g_layers;
    g_layers = l;
}

static void free_layers(void) {
    LayerInfo *l = g_layers;
    while (l) { LayerInfo *n = l->next; free(l); l = n; }
    g_layers = NULL;
}

static int aci_to_hex(int aci) {
    switch (aci) {
        case 1: return 0xFF0000; case 2: return 0xFFFF00;
        case 3: return 0x00FF00; case 4: return 0x00FFFF;
        case 5: return 0x0000FF; case 6: return 0xFF00FF;
        case 7: return 0xFFFFFF; case 8: return 0x808080;
        case 9: return 0xC0C0C0; case 30: return 0xFF8000;
        case 40: return 0xFFBF00; case 50: return 0xFFFF40;
        case 70: return 0x00FF80; case 90: return 0x00FFFF;
        case 130: return 0x0080FF; case 170: return 0x8000FF;
        case 200: return 0xFF0080; case 250: return 0xA0A0A0;
        case 251: return 0x808080; case 252: return 0x606060;
        case 253: return 0x404040; case 254: return 0x202020;
        case 255: return 0x000000;
        default: return 0xCCCCCC;
    }
}

static int adaptive_segments(float radius, float total_angle_rad) {
    if (radius <= 0.0f) return 8;
    float arc_len = fabsf(total_angle_rad) * radius;
    int s = (int)(arc_len / 4.0f);
    if (s < 8) s = 8;
    if (s > 64) s = 64;
    return s;
}

static void tess_transformed_arc_to_batch(BatchTable *table, BatchEntry *batch,
                                           float cx, float cy, float r, float sa, float ea,
                                           float xs, float ys, float cr, float sr,
                                           float ins_x, float ins_y, int has_scale, int has_rot) {
    if (r <= 0.0f) return;
    if (ea <= sa) ea += 360.0f;
    float start = sa * (float)M_PI / 180.0f;
    float end = ea * (float)M_PI / 180.0f;
    int n = adaptive_segments(r * fmaxf(fabsf(xs), fabsf(ys)), end - start);
    if (n < 2) n = 2;
    float step = (end - start) / (float)(n - 1);
    
    float t = start;
    float lx = cx + r * cosf(t);
    float ly = cy + r * sinf(t);
    
    float px = lx, py = ly;
    if (has_scale) { px *= xs; py *= ys; }
    if (has_rot) {
        float tx = px;
        px = tx * cr - py * sr + ins_x;
        py = tx * sr + py * cr + ins_y;
    } else {
        px += ins_x;
        py += ins_y;
    }
    
    for (int i = 1; i < n; i++) {
        t = start + step * (float)i;
        lx = cx + r * cosf(t);
        ly = cy + r * sinf(t);
        
        float x = lx, y = ly;
        if (has_scale) { x *= xs; y *= ys; }
        if (has_rot) {
            float tx = x;
            x = tx * cr - y * sr + ins_x;
            y = tx * sr + y * cr + ins_y;
        } else {
            x += ins_x;
            y += ins_y;
        }
        
        batch_append(table, batch, px, py, x, y);
        px = x; py = y;
    }
}

static void tess_transformed_circle_to_batch(BatchTable *table, BatchEntry *batch,
                                              float cx, float cy, float r,
                                              float xs, float ys, float cr, float sr,
                                              float ins_x, float ins_y, int has_scale, int has_rot) {
    if (r <= 0.0f) return;
    int n = adaptive_segments(r * fmaxf(fabsf(xs), fabsf(ys)), (float)(2.0 * M_PI));
    if (n < 8) n = 8;
    float step = (float)(2.0 * M_PI) / (float)n;
    
    float t = 0.0f;
    float lx = cx + r;
    float ly = cy;
    
    float px = lx, py = ly;
    if (has_scale) { px *= xs; py *= ys; }
    if (has_rot) {
        float tx = px;
        px = tx * cr - py * sr + ins_x;
        py = tx * sr + py * cr + ins_y;
    } else {
        px += ins_x;
        py += ins_y;
    }
    
    for (int i = 1; i <= n; i++) {
        t = step * (float)i;
        lx = cx + r * cosf(t);
        ly = cy + r * sinf(t);
        
        float x = lx, y = ly;
        if (has_scale) { x *= xs; y *= ys; }
        if (has_rot) {
            float tx = x;
            x = tx * cr - y * sr + ins_x;
            y = tx * sr + y * cr + ins_y;
        } else {
            x += ins_x;
            y += ins_y;
        }
        
        batch_append(table, batch, px, py, x, y);
        px = x; py = y;
    }
}

/* ---------- Block operations ---------- */
static BlockDef* get_or_create_block(const char *name) {
    for (BlockDef *b = g_blocks; b; b = b->next) {
        if (_stricmp(b->name, name) == 0) return b;
    }
    BlockDef *b = (BlockDef*)calloc(1, sizeof(BlockDef));
    strncpy(b->name, name, MAX_NAME - 1);
    b->base_x = 0.0f;
    b->base_y = 0.0f;
    b->ents_cap = 64;
    b->ents = (BlockEntity*)malloc(b->ents_cap * sizeof(BlockEntity));
    b->next = g_blocks;
    g_blocks = b;
    return b;
}

static BlockDef* find_block(const char *name) {
    for (BlockDef *b = g_blocks; b; b = b->next) {
        if (_stricmp(b->name, name) == 0) return b;
    }
    return NULL;
}

static BlockEntity* block_add_entity(BlockDef *b, EntType type) {
    if (b->n_ents >= b->ents_cap) {
        b->ents_cap *= 2;
        b->ents = (BlockEntity*)realloc(b->ents, b->ents_cap * sizeof(BlockEntity));
    }
    BlockEntity *e = &b->ents[b->n_ents++];
    memset(e, 0, sizeof(*e));
    e->type = type;
    strncpy(e->layer, "0", MAX_LAYER_NAME - 1);
    e->color = 256;
    return e;
}

static void free_blocks(void) {
    BlockDef *b = g_blocks;
    while (b) { BlockDef *n = b->next; free(b->ents); free(b); b = n; }
    g_blocks = NULL;
}

/* ---------- File reading helpers ---------- */
typedef struct {
    char *data;
    size_t size;
    size_t pos;
} FileBuf;

static int filebuf_open(FileBuf *fb, const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    fseek(f, 0, SEEK_END);
    fb->size = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    fb->data = (char*)malloc(fb->size + 1);
    if (!fb->data) { fclose(f); return -1; }
    size_t r = fread(fb->data, 1, fb->size, f);
    fb->data[r] = 0;
    fb->pos = 0;
    fclose(f);
    return 0;
}

static void filebuf_close(FileBuf *fb) {
    if (fb->data) free(fb->data);
    fb->data = NULL;
}

static int read_line(FileBuf *fb, char *buf, int maxlen) {
    int len = 0;
    while (fb->pos < fb->size && len < maxlen - 1) {
        char c = fb->data[fb->pos];
        if (c == '\n') { fb->pos++; break; }
        if (c != '\r') buf[len++] = c;
        fb->pos++;
    }
    buf[len] = 0;
    int start = 0;
    while (start < len && (buf[start] == ' ' || buf[start] == '\t')) start++;
    if (start > 0) { memmove(buf, buf + start, len - start + 1); len -= start; }
    return len;
}

static float parse_float(const char *s) { return s && *s ? (float)atof(s) : 0.0f; }
static int parse_int(const char *s) { return s && *s ? atoi(s) : 0; }

/* ---------- Apply INSERT (block instance) ---------- */
static void apply_insert(BatchTable *table, const BlockDef *block,
                          float ins_x, float ins_y, float xs, float ys, float rot_deg,
                          int color_override, const char *layer_override) {
    if (!block || block->n_ents == 0) return;
    int has_rot = fabsf(rot_deg) > 1e-6f;
    float rot = rot_deg * (float)M_PI / 180.0f;
    float cr = cosf(rot), sr = sinf(rot);
    int has_scale = (fabsf(xs - 1.0f) > 1e-6f) || (fabsf(ys - 1.0f) > 1e-6f);

    for (int i = 0; i < block->n_ents; i++) {
        BlockEntity *e = &block->ents[i];
        int col = color_override;
        if (col < 0 || col == 256) col = e->color;
        if (col == 256) col = 7;  /* white default */

        const char *lay = e->layer;
        if (strcmp(lay, "0") == 0 && layer_override) {
            lay = layer_override;
        }

        BatchEntry *b = get_or_create_batch(table, lay, col);
        if (!b) continue;

        switch (e->type) {
            case ENT_LINE: {
                float x0 = e->d.line.x1 - block->base_x, y0 = e->d.line.y1 - block->base_y;
                float x1 = e->d.line.x2 - block->base_x, y1 = e->d.line.y2 - block->base_y;
                if (has_scale) { x0 *= xs; y0 *= ys; x1 *= xs; y1 *= ys; }
                if (has_rot) {
                    float tx0 = x0 * cr - y0 * sr + ins_x;
                    float ty0 = x0 * sr + y0 * cr + ins_y;
                    float tx1 = x1 * cr - y1 * sr + ins_x;
                    float ty1 = x1 * sr + y1 * cr + ins_y;
                    batch_append(table, b, tx0, ty0, tx1, ty1);
                } else {
                    batch_append(table, b, x0 + ins_x, y0 + ins_y, x1 + ins_x, y1 + ins_y);
                }
                break;
            }
            case ENT_CIRCLE: {
                float cx = e->d.circle.cx - block->base_x, cy = e->d.circle.cy - block->base_y, r = e->d.circle.r;
                tess_transformed_circle_to_batch(table, b, cx, cy, r, xs, ys, cr, sr, ins_x, ins_y, has_scale, has_rot);
                break;
            }
            case ENT_ARC: {
                float cx = e->d.arc.cx - block->base_x, cy = e->d.arc.cy - block->base_y, r = e->d.arc.r;
                float sa = e->d.arc.sa, ea = e->d.arc.ea;
                tess_transformed_arc_to_batch(table, b, cx, cy, r, sa, ea, xs, ys, cr, sr, ins_x, ins_y, has_scale, has_rot);
                break;
            }
            case ENT_LWPLY: {
                LWPolyData *pd = &e->d.poly;
                int n = pd->n_pts;
                if (n < 2) break;
                float (*pts)[2] = pd->pts;
                float trans[8][2];
                for (int j = 0; j < n; j++) {
                    float x = pts[j][0] - block->base_x, y = pts[j][1] - block->base_y;
                    if (has_scale) { x *= xs; y *= ys; }
                    if (has_rot) {
                        trans[j][0] = x * cr - y * sr + ins_x;
                        trans[j][1] = x * sr + y * cr + ins_y;
                    } else {
                        trans[j][0] = x + ins_x;
                        trans[j][1] = y + ins_y;
                    }
                }
                for (int j = 0; j < n - 1; j++) {
                    batch_append(table, b, trans[j][0], trans[j][1], trans[j+1][0], trans[j+1][1]);
                }
                if (pd->closed) {
                    batch_append(table, b, trans[n-1][0], trans[n-1][1], trans[0][0], trans[0][1]);
                }
                break;
            }
            case ENT_ELLIPSE:
                /* Skip ellipse in INSERT expansion for now (rare) */
                break;
            case ENT_INSERT: {
                BlockDef *nested = find_block(e->d.insert.name);
                if (nested) {
                    float n_xs = e->d.insert.xs * xs;
                    float n_ys = e->d.insert.ys * ys;
                    float n_rot = e->d.insert.rot + rot_deg;
                    float local_x = e->d.insert.x - block->base_x;
                    float local_y = e->d.insert.y - block->base_y;
                    if (has_scale) { local_x *= xs; local_y *= ys; }
                    float n_x, n_y;
                    if (has_rot) {
                        n_x = local_x * cr - local_y * sr + ins_x;
                        n_y = local_x * sr + local_y * cr + ins_y;
                    } else {
                        n_x = local_x + ins_x;
                        n_y = local_y + ins_y;
                    }
                    apply_insert(table, nested, n_x, n_y, n_xs, n_ys, n_rot, col, lay);
                }
                break;
            }
        }
    }
}

/* ---------- Output buffer ---------- */
typedef struct {
    float *verts;
    int *vert_counts;
    char *batch_layers;
    int *batch_colors;
    int n_batches;
    int total_vert_count;
    int total_seg_count;
    char *layer_names;
    int *layer_colors;
    int n_layers;
    float bbox[4];
    float *verts_base;
    char *layer_names_base;
} ParseResult;

/* ---------- Main parser ---------- */
EXPORT
int parse_dxf(const char *filepath, ParseResult *result) {
    FileBuf fb;
    if (filebuf_open(&fb, filepath) != 0) return -1;

    BatchTable table;
    memset(&table, 0, sizeof(table));
    table.bbox_min_x = 1e30f; table.bbox_min_y = 1e30f;
    table.bbox_max_x = -1e30f; table.bbox_max_y = -1e30f;

    char code_line[64];
    char line[1024];
    int current_code = 0;
    int in_entities = 0;
    int in_blocks = 0;
    int in_block_def = 0;
    int expecting_block_name = 0;
    BlockDef *current_block = NULL;

    /* State for current entity in ENTITIES */
    char entity_type[64] = {0};
    char layer[MAX_LAYER_NAME] = "0";
    int color = 256;

    /* LINE */
    float line_x[2] = {0, 0}, line_y[2] = {0, 0};
    int line_pt_idx = 0;
    int last_x_pos = 0, last_y_pos = 0;

    /* ARC/CIRCLE */
    float cx = 0, cy = 0, r = 0, sa = 0, ea = 0;

    /* ELLIPSE */
    float ell_cx = 0, ell_cy = 0, ell_major_x = 0, ell_major_y = 0, ell_ratio = 1.0f;
    float ell_start = 0, ell_end = 0;

    /* LWPOLYLINE */
    int lwpoly_closed = 0;
    int lwpoly_vertex_count = 0;
    float lwpoly_first_x = 0.0f, lwpoly_first_y = 0.0f;
    float lwpoly_prev_x = 0.0f, lwpoly_prev_y = 0.0f;
    float lwpoly_curr_x = 0.0f, lwpoly_curr_y = 0.0f;

    /* SOLID */
    float solid_x[4] = {0.0f}, solid_y[4] = {0.0f};
    int solid_has[4] = {0};

    /* INSERT */
    char ins_block[MAX_NAME] = {0};
    float ins_x = 0, ins_y = 0, ins_xs = 1, ins_ys = 1, ins_rot = 0;

    /* BLOCK */
    float block_base_x = 0.0f, block_base_y = 0.0f;

    int line_num = 0;

    while (fb.pos < fb.size) {
        int len = read_line(&fb, code_line, sizeof(code_line));
        if (len == 0 && fb.pos >= fb.size) break;
        line_num++;
        current_code = parse_int(code_line);

        len = read_line(&fb, line, sizeof(line));
        if (len == 0 && fb.pos >= fb.size) break;
        line_num++;
        if (line[len-1] == '\r') line[--len] = 0;

        if (current_code == 0) {
            /* Process previous entity */
            if (entity_type[0]) {
                if (strcmp(entity_type, "BLOCK") == 0) {
                    if (current_block) {
                        current_block->base_x = block_base_x;
                        current_block->base_y = block_base_y;
                    }
                } else if (in_entities && !in_block_def) {
                    if (strcmp(entity_type, "LINE") == 0) {
                        if (line_pt_idx >= 2) {
                            BatchEntry *b = get_or_create_batch(&table, layer, color);
                            if (b) batch_append(&table, b, line_x[0], line_y[0], line_x[1], line_y[1]);
                        }
                    } else if (strcmp(entity_type, "ARC") == 0) {
                        BatchEntry *b = get_or_create_batch(&table, layer, color);
                        if (b) tess_transformed_arc_to_batch(&table, b, cx, cy, r, sa, ea, 1.0f, 1.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0, 0);
                    } else if (strcmp(entity_type, "CIRCLE") == 0) {
                        BatchEntry *b = get_or_create_batch(&table, layer, color);
                        if (b) tess_transformed_circle_to_batch(&table, b, cx, cy, r, 1.0f, 1.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0, 0);
                    } else if (strcmp(entity_type, "LWPOLYLINE") == 0 || strcmp(entity_type, "POLYLINE") == 0) {
                        if (lwpoly_closed && lwpoly_vertex_count >= 2) {
                            BatchEntry *b = get_or_create_batch(&table, layer, color);
                            if (b) batch_append(&table, b, lwpoly_prev_x, lwpoly_prev_y, lwpoly_first_x, lwpoly_first_y);
                        }
                    } else if (strcmp(entity_type, "INSERT") == 0) {
                        BlockDef *bd = find_block(ins_block);
                        if (bd) {
                            apply_insert(&table, bd, ins_x, ins_y, ins_xs, ins_ys, ins_rot, color, layer);
                        }
                    } else if (strcmp(entity_type, "ELLIPSE") == 0) {
                        float a = sqrtf(ell_major_x * ell_major_x + ell_major_y * ell_major_y);
                        float b = a * ell_ratio;
                        if (a > 0 && b > 0) {
                            float start = ell_start;
                            float end = ell_end;
                            if (end - start < 0.001f) end = start + (float)(2.0 * M_PI);
                            if (end < start) end += (float)(2.0 * M_PI);
                            int n = adaptive_segments(a > b ? a : b, end - start);
                            if (n < 8) n = 8;
                            float erot = atan2f(ell_major_y, ell_major_x);
                            float cosr = cosf(erot), sinr = sinf(erot);
                            float step = (end - start) / (float)(n - 1);
                            float px = ell_cx + a * cosf(start) * cosr - b * sinf(start) * sinr;
                            float py = ell_cy + a * cosf(start) * sinr + b * sinf(start) * cosr;
                            BatchEntry *be = get_or_create_batch(&table, layer, color);
                            if (be) {
                                for (int j = 1; j < n; j++) {
                                    float t = start + step * (float)j;
                                    float x = ell_cx + a * cosf(t) * cosr - b * sinf(t) * sinr;
                                    float y = ell_cy + a * cosf(t) * sinr + b * sinf(t) * cosr;
                                    batch_append(&table, be, px, py, x, y);
                                    px = x; py = y;
                                }
                            }
                        }
                    } else if (strcmp(entity_type, "SOLID") == 0) {
                        if (solid_has[0] && solid_has[1] && solid_has[2]) {
                            float x1 = solid_x[0], y1 = solid_y[0];
                            float x2 = solid_x[1], y2 = solid_y[1];
                            float x3 = solid_x[2], y3 = solid_y[2];
                            float x4 = solid_has[3] ? solid_x[3] : x3;
                            float y4 = solid_has[3] ? solid_y[3] : y3;
                            BatchEntry *b = get_or_create_batch(&table, layer, color);
                            if (b) {
                                batch_append(&table, b, x1, y1, x2, y2);
                                batch_append(&table, b, x2, y2, x4, y4);
                                batch_append(&table, b, x4, y4, x3, y3);
                                batch_append(&table, b, x3, y3, x1, y1);
                            }
                        }
                    }
                } else if (in_block_def && current_block) {
                    /* Save entity in block */
                    if (strcmp(entity_type, "LINE") == 0 && line_pt_idx >= 2) {
                        BlockEntity *be = block_add_entity(current_block, ENT_LINE);
                        if (be) {
                            strncpy(be->layer, layer, MAX_LAYER_NAME - 1);
                            be->color = color;
                            be->d.line.x1 = line_x[0]; be->d.line.y1 = line_y[0];
                            be->d.line.x2 = line_x[1]; be->d.line.y2 = line_y[1];
                        }
                    } else if (strcmp(entity_type, "ARC") == 0) {
                        BlockEntity *be = block_add_entity(current_block, ENT_ARC);
                        if (be) {
                            strncpy(be->layer, layer, MAX_LAYER_NAME - 1);
                            be->color = color;
                            be->d.arc.cx = cx; be->d.arc.cy = cy;
                            be->d.arc.r = r; be->d.arc.sa = sa; be->d.arc.ea = ea;
                        }
                    } else if (strcmp(entity_type, "CIRCLE") == 0) {
                        BlockEntity *be = block_add_entity(current_block, ENT_CIRCLE);
                        if (be) {
                            strncpy(be->layer, layer, MAX_LAYER_NAME - 1);
                            be->color = color;
                            be->d.circle.cx = cx; be->d.circle.cy = cy; be->d.circle.r = r;
                        }
                    } else if (strcmp(entity_type, "LWPOLYLINE") == 0 || strcmp(entity_type, "POLYLINE") == 0) {
                        if (lwpoly_closed && lwpoly_vertex_count >= 2) {
                            BlockEntity *be = block_add_entity(current_block, ENT_LINE);
                            if (be) {
                                strncpy(be->layer, layer, MAX_LAYER_NAME - 1);
                                be->color = color;
                                be->d.line.x1 = lwpoly_prev_x;
                                be->d.line.y1 = lwpoly_prev_y;
                                be->d.line.x2 = lwpoly_first_x;
                                be->d.line.y2 = lwpoly_first_y;
                            }
                        }
                    } else if (strcmp(entity_type, "INSERT") == 0) {
                        BlockEntity *be = block_add_entity(current_block, ENT_INSERT);
                        if (be) {
                            strncpy(be->layer, layer, MAX_LAYER_NAME - 1);
                            be->color = color;
                            strncpy(be->d.insert.name, ins_block, MAX_NAME - 1);
                            be->d.insert.name[MAX_NAME - 1] = 0;
                            be->d.insert.x = ins_x;
                            be->d.insert.y = ins_y;
                            be->d.insert.xs = ins_xs;
                            be->d.insert.ys = ins_ys;
                            be->d.insert.rot = ins_rot;
                        }
                    } else if (strcmp(entity_type, "SOLID") == 0) {
                        if (solid_has[0] && solid_has[1] && solid_has[2]) {
                            float x1 = solid_x[0], y1 = solid_y[0];
                            float x2 = solid_x[1], y2 = solid_y[1];
                            float x3 = solid_x[2], y3 = solid_y[2];
                            float x4 = solid_has[3] ? solid_x[3] : x3;
                            float y4 = solid_has[3] ? solid_y[3] : y3;
                            float lines[4][4] = {
                                {x1, y1, x2, y2},
                                {x2, y2, x4, y4},
                                {x4, y4, x3, y3},
                                {x3, y3, x1, y1}
                            };
                            int limit = solid_has[3] ? 4 : 3;
                            if (limit == 3) {
                                lines[1][2] = x3; lines[1][3] = y3;
                                lines[2][0] = x3; lines[2][1] = y3;
                            }
                            for (int k = 0; k < limit; k++) {
                                BlockEntity *be = block_add_entity(current_block, ENT_LINE);
                                if (be) {
                                    strncpy(be->layer, layer, MAX_LAYER_NAME - 1);
                                    be->color = color;
                                    be->d.line.x1 = lines[k][0];
                                    be->d.line.y1 = lines[k][1];
                                    be->d.line.x2 = lines[k][2];
                                    be->d.line.y2 = lines[k][3];
                                }
                            }
                        }
                    }
                }
            }

            /* Handle section/entity markers */
            if (strcmp(line, "SECTION") == 0) {
                int l2 = read_line(&fb, code_line, sizeof(code_line));
                int l3 = read_line(&fb, line, sizeof(line));
                if (line[strlen(line)-1] == '\r') line[strlen(line)-1] = 0;
                if (l2 > 0 && parse_int(code_line) == 2) {
                    if (strcmp(line, "ENTITIES") == 0) { in_entities = 1; in_blocks = 0; }
                    else if (strcmp(line, "BLOCKS") == 0) { in_entities = 0; in_blocks = 1; }
                    else { in_entities = 0; in_blocks = 0; }
                }
                entity_type[0] = 0;
                continue;
            } else if (strcmp(line, "ENDSEC") == 0) {
                in_entities = 0; in_blocks = 0;
                if (in_block_def) { in_block_def = 0; current_block = NULL; }
                entity_type[0] = 0;
                continue;
            } else if (strcmp(line, "EOF") == 0) {
                break;
            } else if (strcmp(line, "BLOCK") == 0) {
                in_block_def = 1;
                expecting_block_name = 1;
                strncpy(entity_type, "BLOCK", sizeof(entity_type) - 1);
                block_base_x = 0.0f;
                block_base_y = 0.0f;
                continue;
            } else if (strcmp(line, "ENDBLK") == 0) {
                in_block_def = 0;
                expecting_block_name = 0;
                current_block = NULL;
                entity_type[0] = 0;
                continue;
            }

            /* New entity */
            if (in_entities && !in_block_def) {
                if (line[0] == 'L' || line[0] == 'A' || line[0] == 'C' || line[0] == 'P' ||
                    line[0] == 'S' || line[0] == 'E' || line[0] == 'I' || line[0] == 'H' ||
                    line[0] == 'T' || line[0] == 'D' || line[0] == 'M') {
                    strncpy(entity_type, line, sizeof(entity_type) - 1);
                    entity_type[sizeof(entity_type) - 1] = 0;
                    strncpy(layer, "0", MAX_LAYER_NAME);
                    color = 256;
                    line_pt_idx = 0;
                    cx = cy = r = sa = ea = 0;
                    ell_cx = ell_cy = ell_major_x = ell_major_y = 0;
                    ell_ratio = 1.0f; ell_start = 0; ell_end = (float)(2.0 * M_PI);
                    lwpoly_closed = 0; lwpoly_vertex_count = 0;
                    memset(solid_has, 0, sizeof(solid_has));
                    ins_block[0] = 0; ins_x = ins_y = 0; ins_xs = ins_ys = 1; ins_rot = 0;
                } else {
                    entity_type[0] = 0;
                }
            } else if (in_blocks) {
                /* New entity in BLOCKS section */
                if (line[0] == 'L' || line[0] == 'A' || line[0] == 'C' || line[0] == 'P' ||
                    line[0] == 'S' || line[0] == 'E' || line[0] == 'I') {
                    strncpy(entity_type, line, sizeof(entity_type) - 1);
                    entity_type[sizeof(entity_type) - 1] = 0;
                    strncpy(layer, "0", MAX_LAYER_NAME);
                    color = 256;
                    line_pt_idx = 0;
                    cx = cy = r = sa = ea = 0;
                    ell_cx = ell_cy = ell_major_x = ell_major_y = 0;
                    ell_ratio = 1.0f; ell_start = 0; ell_end = (float)(2.0 * M_PI);
                    lwpoly_closed = 0; lwpoly_vertex_count = 0;
                    memset(solid_has, 0, sizeof(solid_has));
                    ins_block[0] = 0; ins_x = ins_y = 0; ins_xs = ins_ys = 1; ins_rot = 0;
                } else {
                    entity_type[0] = 0;
                }
            } else {
                entity_type[0] = 0;
            }
            continue;
        }

        if (!entity_type[0]) continue;

        /* Handle entity attributes by code */
        if (current_code == 8) {
            strncpy(layer, line, MAX_LAYER_NAME - 1);
            layer[MAX_LAYER_NAME - 1] = 0;
            register_layer(layer, -1);
        } else if (current_code == 62) {
            color = parse_int(line);
        } else if (current_code == 70) {
            if (strcmp(entity_type, "LWPOLYLINE") == 0 || strcmp(entity_type, "POLYLINE") == 0) {
                int lwpoly_flags = parse_int(line);
                lwpoly_closed = (lwpoly_flags & 1) != 0;
            }
        } else if (current_code == 90) {
            /* Vertex count for LWPOLYLINE - just a hint, we read as we go */
        } else if (current_code == 10) {
            float v = parse_float(line);
            if (strcmp(entity_type, "LINE") == 0) {
                if (line_pt_idx < 2) { line_x[line_pt_idx] = v; last_x_pos = line_pt_idx; }
            } else if (strcmp(entity_type, "ARC") == 0 || strcmp(entity_type, "CIRCLE") == 0) {
                cx = v;
            } else if (strcmp(entity_type, "LWPOLYLINE") == 0 || strcmp(entity_type, "POLYLINE") == 0) {
                lwpoly_curr_x = v;
            } else if (strcmp(entity_type, "ELLIPSE") == 0) {
                ell_cx = v;
            } else if (strcmp(entity_type, "INSERT") == 0) {
                ins_x = v;
            } else if (strcmp(entity_type, "SOLID") == 0) {
                solid_x[0] = v; solid_has[0] = 1;
            } else if (strcmp(entity_type, "BLOCK") == 0) {
                block_base_x = v;
            }
        } else if (current_code == 20) {
            float v = parse_float(line);
            if (strcmp(entity_type, "LINE") == 0) {
                if (line_pt_idx < 2) { line_y[line_pt_idx] = v; last_y_pos = line_pt_idx; }
            } else if (strcmp(entity_type, "ARC") == 0 || strcmp(entity_type, "CIRCLE") == 0) {
                cy = v;
            } else if (strcmp(entity_type, "LWPOLYLINE") == 0 || strcmp(entity_type, "POLYLINE") == 0) {
                lwpoly_curr_y = v;
 
                /* Complete vertex */
                if (lwpoly_vertex_count == 0) {
                    lwpoly_first_x = lwpoly_curr_x;
                    lwpoly_first_y = lwpoly_curr_y;
                } else {
                    if (in_entities && !in_block_def) {
                        BatchEntry *b = get_or_create_batch(&table, layer, color);
                        if (b) batch_append(&table, b, lwpoly_prev_x, lwpoly_prev_y, lwpoly_curr_x, lwpoly_curr_y);
                    } else if (in_block_def && current_block) {
                        BlockEntity *be = block_add_entity(current_block, ENT_LINE);
                        if (be) {
                            strncpy(be->layer, layer, MAX_LAYER_NAME - 1);
                            be->color = color;
                            be->d.line.x1 = lwpoly_prev_x;
                            be->d.line.y1 = lwpoly_prev_y;
                            be->d.line.x2 = lwpoly_curr_x;
                            be->d.line.y2 = lwpoly_curr_y;
                        }
                    }
                }
                lwpoly_prev_x = lwpoly_curr_x;
                lwpoly_prev_y = lwpoly_curr_y;
                lwpoly_vertex_count++;
            } else if (strcmp(entity_type, "ELLIPSE") == 0) {
                ell_cy = v;
            } else if (strcmp(entity_type, "INSERT") == 0) {
                ins_y = v;
            } else if (strcmp(entity_type, "SOLID") == 0) {
                solid_y[0] = v; solid_has[0] = 1;
            } else if (strcmp(entity_type, "BLOCK") == 0) {
                block_base_y = v;
            }
        } else if (current_code == 11) {
            float v = parse_float(line);
            if (strcmp(entity_type, "LINE") == 0) {
                line_x[1] = v;
            } else if (strcmp(entity_type, "ELLIPSE") == 0) {
                ell_major_x = v;
            } else if (strcmp(entity_type, "SOLID") == 0) {
                solid_x[1] = v; solid_has[1] = 1;
            }
        } else if (current_code == 21) {
            float v = parse_float(line);
            if (strcmp(entity_type, "LINE") == 0) {
                line_y[1] = v;
                line_pt_idx = 2;
            } else if (strcmp(entity_type, "ELLIPSE") == 0) {
                ell_major_y = v;
            } else if (strcmp(entity_type, "SOLID") == 0) {
                solid_y[1] = v; solid_has[1] = 1;
            }
        } else if (current_code == 12) {
            float v = parse_float(line);
            if (strcmp(entity_type, "SOLID") == 0) {
                solid_x[2] = v; solid_has[2] = 1;
            }
        } else if (current_code == 22) {
            float v = parse_float(line);
            if (strcmp(entity_type, "SOLID") == 0) {
                solid_y[2] = v; solid_has[2] = 1;
            }
        } else if (current_code == 13) {
            float v = parse_float(line);
            if (strcmp(entity_type, "SOLID") == 0) {
                solid_x[3] = v; solid_has[3] = 1;
            }
        } else if (current_code == 23) {
            float v = parse_float(line);
            if (strcmp(entity_type, "SOLID") == 0) {
                solid_y[3] = v; solid_has[3] = 1;
            }
        } else if (current_code == 30) {
            /* Z coord - ignored or handled if we want, but not needed for basic 2D */
        } else if (current_code == 40) {
            float v = parse_float(line);
            if (strcmp(entity_type, "ARC") == 0 || strcmp(entity_type, "CIRCLE") == 0) r = v;
            else if (strcmp(entity_type, "ELLIPSE") == 0) ell_ratio = v;
            else if (strcmp(entity_type, "INSERT") == 0) ins_xs = v;
        } else if (current_code == 50) {
            float v = parse_float(line);
            if (strcmp(entity_type, "ARC") == 0) sa = v;
            else if (strcmp(entity_type, "INSERT") == 0) ins_rot = v;
            else if (strcmp(entity_type, "ELLIPSE") == 0) ell_start = v;
        } else if (current_code == 51) {
            float v = parse_float(line);
            if (strcmp(entity_type, "ARC") == 0) ea = v;
            else if (strcmp(entity_type, "ELLIPSE") == 0) ell_end = v;
        } else if (current_code == 2) {
            /* Block name (INSERT) or block start name */
            if (expecting_block_name) {
                current_block = get_or_create_block(line);
                expecting_block_name = 0;
            } else if (strcmp(entity_type, "INSERT") == 0) {
                strncpy(ins_block, line, MAX_NAME - 1);
                ins_block[MAX_NAME - 1] = 0;
            }
        } else if (current_code == 41) {
            if (strcmp(entity_type, "INSERT") == 0) ins_xs = parse_float(line);
        } else if (current_code == 42) {
            if (strcmp(entity_type, "INSERT") == 0) ins_ys = parse_float(line);
        }
    }

    filebuf_close(&fb);

    /* Serialize result */
    int n_batches = table.batch_count;
    int total_verts = 0;
    for (int i = 0; i < BATCH_TABLE_SIZE; i++) {
        for (BatchEntry *e = table.buckets[i]; e; e = e->next) {
            total_verts += e->vert_count;
        }
    }

    result->n_batches = n_batches;
    result->total_vert_count = total_verts;
    result->total_seg_count = table.total_segments;
    result->bbox[0] = (table.bbox_min_x == 1e30f) ? 0.0f : table.bbox_min_x;
    result->bbox[1] = (table.bbox_min_y == 1e30f) ? 0.0f : table.bbox_min_y;
    result->bbox[2] = (table.bbox_max_x == -1e30f) ? 0.0f : table.bbox_max_x;
    result->bbox[3] = (table.bbox_max_y == -1e30f) ? 0.0f : table.bbox_max_y;

    result->vert_counts = (int*)malloc((size_t)n_batches * sizeof(int));
    result->batch_layers = (char*)calloc((size_t)n_batches, MAX_LAYER_NAME);
    result->batch_colors = (int*)malloc((size_t)n_batches * sizeof(int));
    result->verts_base = (float*)malloc((size_t)total_verts * sizeof(float));
    result->verts = result->verts_base;

    float *vout = result->verts_base;
    int bi = 0;
    for (int i = 0; i < BATCH_TABLE_SIZE; i++) {
        for (BatchEntry *e = table.buckets[i]; e; e = e->next) {
            result->vert_counts[bi] = e->vert_count;
            memcpy(vout, e->verts, e->vert_count * sizeof(float));
            vout += e->vert_count;
            strncpy(result->batch_layers + (size_t)bi * MAX_LAYER_NAME, e->layer, MAX_LAYER_NAME);
            result->batch_colors[bi] = e->color;
            bi++;
        }
    }

    int n_layers = 0;
    for (LayerInfo *l = g_layers; l; l = l->next) n_layers++;
    result->n_layers = n_layers;
    size_t layer_names_size = (size_t)n_layers * MAX_LAYER_NAME;
    result->layer_names_base = (char*)calloc(n_layers, MAX_LAYER_NAME);
    result->layer_names = result->layer_names_base;
    result->layer_colors = (int*)malloc((size_t)n_layers * sizeof(int));
    int li = 0;
    for (LayerInfo *l = g_layers; l; l = l->next) {
        strncpy(result->layer_names + (size_t)li * MAX_LAYER_NAME, l->name, MAX_LAYER_NAME);
        result->layer_colors[li] = l->color;
        li++;
    }

    free_batch_table(&table);
    free_layers();
    free_blocks();

    return 0;
}

EXPORT
void free_dxf_result(ParseResult *result) {
    if (result->verts_base) free(result->verts_base);
    if (result->vert_counts) free(result->vert_counts);
    if (result->batch_layers) free(result->batch_layers);
    if (result->batch_colors) free(result->batch_colors);
    if (result->layer_names_base) free(result->layer_names_base);
    if (result->layer_colors) free(result->layer_colors);
    memset(result, 0, sizeof(*result));
}