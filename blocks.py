'''blocks.py — the "blocks" startup splash for configsys, as a code plugin.

Falling coloured blocks, Tetris-ish but freed of the 4-cell limit: shapes of 3–10 blocks tumble
from the top and stack up, completing rows along the bottom and filling the screen. Unlike Tetris
no rows are cleared — the heap only grows. Multiple shapes fall at once and more keep coming until
the screen is full. Each block cell is TWO ascii blocks side-by-side (≈ a square on a modern
terminal). PROGRESS drives the fill: the screen finishes filling just as inspection completes.

The trick (see the module docstring for BlocksSim): we first solve a COMPLETE tiling of the whole
grid into polyominoes — the invisible "compacted" final state — then replay it, dropping each piece
into its solved slot in a gravity-respecting order (a piece falls only once everything beneath it
has settled). That guarantees the screen fills, that no piece ever falls through another, and — by
counting pieces/cells — lets us pace releases so the fill lands on time.

Ships as a configsys splash provider (see configsys/splashes.py for the ABI): the HOST
(configsys.tui.splash.run_splash) owns the frame loop and calls render(frame); BlocksSim is the
curses-free, deterministic sim (unit-tested); BlocksSplash the curses renderer. `SPLASHES` at the
foot exports it so the trusted loader registers `splash: blocks`.

Colour budget: curses `color_pair()` is 8-bit and the host does NOT recycle pairs between frames,
so a run must stay < 256 distinct pairs. There can be ~150+ pieces, so pieces can't each own a
colour; instead we use a FIXED palette of PALETTE_HUES hues (× 3 bevel shades = the only pairs) and
assign pieces by greedy graph-colouring that maximises hue contrast between neighbours.
'''

import colorsys
import curses

from configsys.plugins import Splash

FPS = 30.0
MIN_DURATION = 0.8
PALETTE_HUES = 24          # distinct hues in the fixed palette (× 3 shades = pairs used)
MIN_PIECE, MAX_PIECE = 3, 10
DROP_ROWS = 10             # a piece spawns at most this many rows above its slot (a short, snappy
                           # drop). Full-height drops serialised by support depth take ~10s; capping
                           # the fall keeps the cascade tail ~1s so a short inspection barely overruns.
BLOCK = '█'                # one cell = BLOCK * 2 (two side-by-side ≈ a square)

# bevel shades per hue: a lit top edge, a shadowed bottom/right edge, flat interior — a 3D stack look
SH_BASE, SH_LIGHT, SH_DARK = 0, 1, 2


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def _hue_dist(a, b):
    '''Circular distance between two hues in [0,1) — 0.5 is opposite on the wheel.'''
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


class _Piece:
    __slots__ = ('cells', 'top', 'bottom', 'color', 'need', 'state', 'offset')

    def __init__(self, cells):
        self.cells = cells                       # [(r, c), ...] in the solved (final) tiling
        self.top = min(r for r, _ in cells)
        self.bottom = max(r for r, _ in cells)
        self.color = 0                           # palette hue index (assigned for contrast)
        self.need = 0                            # unsettled supporters remaining (0 => releasable)
        self.state = 0                           # 0 waiting, 1 falling, 2 settled
        self.offset = 0.0                        # rows still above the final slot while falling


# piece states
WAIT, FALL, SET = 0, 1, 2


class BlocksSim:
    '''Curses-free falling-blocks state, deterministic given `rng`.

    Construction SOLVES the endgame: partition the gw×gh grid into connected polyominoes
    (`_partition`), assign contrast colours (`_colorize`), and build the support graph
    (`_build_supports`) — piece B supports A when a B-cell is directly beneath an A-cell. A piece is
    releasable only when all its supporters have settled, which keeps its straight-down path clear.

    Feed progress via set_progress(frac); advance with step(dt). Releases are paced so
    settled+in-flight cells track progress×total, and `filled` is True once every cell has settled.
    Read `settled_color`/`settled_shade` (the heap) and `falling` (piece indices in flight) to draw.
    '''

    def __init__(self, gw, gh, rng):
        self.gw = max(1, int(gw))
        self.gh = max(1, int(gh))
        self.rng = rng
        self.total_cells = self.gw * self.gh
        self.settled_cells = 0
        self._p = 0.0
        self.falling = []
        # a snappy fall: DROP_ROWS rows in ~0.15s. Fast enough that the settle-gated cascade tail
        # after inspection finishes stays ~1s (see DROP_ROWS); the done-boost halves it again.
        self.fall_speed = max(45.0, self.gh * 3.0)

        self.cell_piece = [[-1] * self.gw for _ in range(self.gh)]
        self.pieces = []
        self._partition()
        self._shade_grid = self._compute_shades()
        self._colorize()
        self._build_supports()

        # the heap, stamped as pieces settle
        self.settled_color = [[-1] * self.gw for _ in range(self.gh)]
        self.settled_shade = [[0] * self.gw for _ in range(self.gh)]

    # -- endgame solver ---------------------------------------------------

    def _neighbors(self, r, c):
        if r > 0:
            yield r - 1, c
        if r + 1 < self.gh:
            yield r + 1, c
        if c > 0:
            yield r, c - 1
        if c + 1 < self.gw:
            yield r, c + 1

    def _partition(self):
        '''Tile the whole grid with connected polyominoes that are DROPPABLE BY CONSTRUCTION —
        surface accretion. Always start the next piece at the surface of the currently-lowest
        column, and only ever add a cell whose cell directly below is already filled (floor or heap).

        Two invariants fall out, both essential:
          * no holes — we never cover an empty cell, and we always fill the lowest gap first, so the
            board fills bottom-up with no trapped space; and
          * no support cycles — every piece rests on the floor or on EARLIER pieces, so the
            generation order is itself a valid gravity drop order (piece i never rests on piece j>i).
        An arbitrary tiling has neither: pieces meeting along a jagged border mutually support each
        other, and the resulting cyclic support graph has no clean drop order at all.'''
        gh, gw, cp = self.gh, self.gw, self.cell_piece
        filled = [[False] * gw for _ in range(gh)]
        height = [0] * gw                                # filled cells from the bottom, per column

        def supported(r, c):
            return not filled[r][c] and (r + 1 >= gh or filled[r + 1][c])

        self.pieces = []
        placed = 0
        while placed < self.total_cells:
            c0 = min((c for c in range(gw) if height[c] < gh), key=lambda c: (height[c], c))
            r0 = gh - 1 - height[c0]
            pid = len(self.pieces)
            cells = [(r0, c0)]
            filled[r0][c0] = True
            cp[r0][c0] = pid
            target = self.rng.randint(MIN_PIECE, MAX_PIECE)
            while len(cells) < target:
                cand = sorted({(nr, nc) for (r, c) in cells for nr, nc in self._neighbors(r, c)
                               if supported(nr, nc)})
                if not cand:
                    break                                # boxed in — a small piece, still valid
                nr, nc = cand[self.rng.randrange(len(cand))]
                cells.append((nr, nc))
                filled[nr][nc] = True
                cp[nr][nc] = pid
            for c in {c for _, c in cells}:              # advance each touched column's surface
                while height[c] < gh and filled[gh - 1 - height[c]][c]:
                    height[c] += 1
            self.pieces.append(_Piece(cells))
            placed += len(cells)

    def _compute_shades(self):
        '''Per-cell bevel shade: lit top edge, shadowed bottom/right edge, flat interior.'''
        cp = self.cell_piece
        grid = [[SH_BASE] * self.gw for _ in range(self.gh)]
        for r in range(self.gh):
            for c in range(self.gw):
                pid = cp[r][c]
                above_same = r > 0 and cp[r - 1][c] == pid
                below_diff = r + 1 >= self.gh or cp[r + 1][c] != pid
                right_diff = c + 1 >= self.gw or cp[r][c + 1] != pid
                if not above_same:
                    grid[r][c] = SH_LIGHT
                elif below_diff or right_diff:
                    grid[r][c] = SH_DARK
        return grid

    def _adjacent_pieces(self, pid):
        seen = set()
        for (r, c) in self.pieces[pid].cells:
            for nr, nc in self._neighbors(r, c):
                q = self.cell_piece[nr][nc]
                if q != pid:
                    seen.add(q)
        return seen

    def _colorize(self):
        '''Greedy contrast colouring: give each piece the hue index farthest (on the wheel) from its
        already-coloured neighbours, so adjacent pieces are visibly different.'''
        hues = [k / PALETTE_HUES for k in range(PALETTE_HUES)]
        chosen = [None] * len(self.pieces)
        for pid in range(len(self.pieces)):
            nb_hues = [hues[chosen[q]] for q in self._adjacent_pieces(pid)
                       if 0 <= q < len(self.pieces) and chosen[q] is not None]
            if not nb_hues:
                k = self.rng.randrange(PALETTE_HUES)
            else:
                # maximise the minimum distance to neighbour hues; random tie-break for variety
                best, best_d = [], -1.0
                for k in range(PALETTE_HUES):
                    d = min(_hue_dist(hues[k], nh) for nh in nb_hues)
                    if d > best_d + 1e-9:
                        best, best_d = [k], d
                    elif abs(d - best_d) <= 1e-9:
                        best.append(k)
                k = best[self.rng.randrange(len(best))]
            chosen[pid] = k
            self.pieces[pid].color = k

    def _build_supports(self):
        '''For each piece, count distinct supporters (pieces with a cell directly beneath one of
        ours) and record dependents, so settling a piece can free the ones resting on it. A piece
        supported only by the floor starts releasable (need == 0).'''
        self.dependents = [[] for _ in self.pieces]
        for pid, pc in enumerate(self.pieces):
            supporters = set()
            for (r, c) in pc.cells:
                if r + 1 < self.gh:
                    q = self.cell_piece[r + 1][c]
                    if q != pid:
                        supporters.add(q)
            pc.need = len(supporters)
            for q in supporters:
                self.dependents[q].append(pid)

    # -- runtime ----------------------------------------------------------

    def set_progress(self, frac):
        '''Monotonic 0..1 fill target (never rewinds).'''
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        self._p = max(self._p, frac)

    @property
    def filled(self):
        return self.settled_cells >= self.total_cells

    def _release(self, pid):
        pc = self.pieces[pid]
        pc.state = FALL
        pc.offset = float(min(pc.top + 1, DROP_ROWS))    # a short drop into the slot (capped)
        self.falling.append(pid)

    def _settle(self, pid):
        pc = self.pieces[pid]
        pc.state = SET
        pc.offset = 0.0
        for (r, c) in pc.cells:
            self.settled_color[r][c] = pc.color
            self.settled_shade[r][c] = self._shade_grid[r][c]
        self.settled_cells += len(pc.cells)
        for dep in self.dependents[pid]:
            self.pieces[dep].need -= 1

    def step(self, dt):
        if dt <= 0:
            return
        speed = self.fall_speed * (2.0 if self._p >= 0.999 else 1.0)
        # advance the fallers; settle any that have reached their slot
        still = []
        for pid in self.falling:
            pc = self.pieces[pid]
            pc.offset -= speed * dt
            if pc.offset <= 0.0:
                self._settle(pid)
            else:
                still.append(pid)
        self.falling = still

        inflight = sum(len(self.pieces[p].cells) for p in self.falling)
        want = self._p * self.total_cells
        released_any = False
        # releasable = waiting pieces whose supporters have all settled; lowest slots first
        releasable = sorted((p for p, pc in enumerate(self.pieces) if pc.state == WAIT and pc.need <= 0),
                            key=lambda p: -self.pieces[p].bottom)
        for pid in releasable:
            if self.settled_cells + inflight >= want:
                break
            self._release(pid)
            inflight += len(self.pieces[pid].cells)
            released_any = True
        # anti-stall / cycle-break: nothing in flight but work owed and a dependency cycle blocks
        # every releasable piece -> force the lowest waiting piece so the fill can't deadlock.
        if not self.falling and not released_any and self.settled_cells < self.total_cells and want > self.settled_cells:
            waiting = [p for p, pc in enumerate(self.pieces) if pc.state == WAIT]
            if waiting:
                self._release(max(waiting, key=lambda p: self.pieces[p].bottom))


class BlocksSplash(Splash):
    '''The `blocks` splash: a curses driver around a BlocksSim. Pre-bakes the fixed hue×shade
    palette once (bounded, well under the 255-pair ceiling), then render(frame) advances the sim
    toward frame.progress and paints the settled heap plus the pieces in flight. The host owns the
    loop (run_splash) — this just draws one frame and reports whether the screen is full.'''

    name = 'blocks'
    fps = FPS
    min_duration = MIN_DURATION

    def __init__(self, scr, pal, size, seed=None):
        super().__init__(scr, pal, size, seed)
        self.gw = max(1, self.w // 2)                    # each cell is two chars wide
        self.gh = max(1, self.h)
        self.sim = BlocksSim(self.gw, self.gh, self.rng)
        self._attr = self._bake_palette(pal)
        self._label_attr = pal.rgb_pair((238, 238, 246), (12, 12, 18)) | curses.A_BOLD

    def _bake_palette(self, pal):
        '''[hue][shade] -> curses attr. base / lit-top / shadowed-edge per hue. The ONLY pairs the
        run allocates (PALETTE_HUES × 3), so the pair count is fixed regardless of piece count.'''
        attr = []
        for k in range(PALETTE_HUES):
            h = k / PALETTE_HUES
            base = pal.rgb_attr(_hsv(h, 0.62, 0.82))
            light = pal.rgb_attr(_hsv(h, 0.42, 0.98)) | curses.A_BOLD
            dark = pal.rgb_attr(_hsv(h, 0.72, 0.52))
            attr.append((base, light, dark))
        return attr

    def render(self, frame):
        self.sim.set_progress(frame.progress)
        self.sim.step(frame.dt)
        sim, scr = self.sim, self.scr
        scr.erase()
        cell = BLOCK * 2
        # the settled heap
        sc, ss = sim.settled_color, sim.settled_shade
        for r in range(sim.gh):
            row_c, row_s = sc[r], ss[r]
            for c in range(sim.gw):
                col = row_c[c]
                if col >= 0:
                    self._add(r, c * 2, cell, self._attr[col][row_s[c]])
        # pieces in flight, drawn at their current fall offset
        for pid in sim.falling:
            pc = sim.pieces[pid]
            drop = int(pc.offset)
            attrs = self._attr[pc.color]
            for (r, c) in pc.cells:
                rr = r - drop
                if 0 <= rr < sim.gh:
                    self._add(rr, c * 2, cell, attrs[sim._shade_grid[r][c]])
        if frame.label:
            self._draw_label(frame)
        return sim.filled

    def _label_text(self, counts, label):
        i, total = counts
        if not total:
            return f'{label}…'
        return f'{label}:   {i}/{total} ({int(i / total * 100)}%)'

    def _draw_label(self, frame):
        text = ' ' + self._label_text(frame.counts, frame.label) + ' '
        y = max(0, self.h // 2)
        x = max(0, (self.w - len(text)) // 2)
        for k, ch in enumerate(text):
            if x + k < self.w:
                self._add(y, x + k, ch, self._label_attr)

    def _add(self, y, x, s, attr):
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:                             # the last cell always throws — a curses fact
            pass


SPLASHES = [BlocksSplash]

