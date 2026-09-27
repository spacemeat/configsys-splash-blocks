'''Unit tests for the blocks splash simulation (configsys-splash-blocks plugin).

Only the curses-free core (BlocksSim: the tiling solver, colouring, support graph, and fill
runtime) is tested here; BlocksSplash.render drives curses and is exercised by hand. The sim is
deterministic given its rng, so these assertions are stable.'''

import importlib.util
import pathlib
import random
import types

_p = pathlib.Path(__file__).resolve().parent.parent / 'blocks.py'
_spec = importlib.util.spec_from_file_location('blocks', _p)
blocks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(blocks)


def _sim(gw=24, gh=14, seed=0):
    return blocks.BlocksSim(gw, gh, random.Random(seed))


def _connected(sim, cells):
    cellset = set(cells)
    seen = {cells[0]}
    stack = [cells[0]]
    while stack:
        r, c = stack.pop()
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if (nr, nc) in cellset and (nr, nc) not in seen:
                seen.add((nr, nc))
                stack.append((nr, nc))
    return len(seen) == len(cellset)


# -- the tiling solver ----------------------------------------------------

def test_partition_covers_every_cell_exactly_once():
    for seed in range(8):
        sim = _sim(seed=seed)
        seen = [[0] * sim.gw for _ in range(sim.gh)]
        for pid, pc in enumerate(sim.pieces):
            for (r, c) in pc.cells:
                assert sim.cell_piece[r][c] == pid       # grid label agrees with the piece list
                seen[r][c] += 1
        assert all(seen[r][c] == 1 for r in range(sim.gh) for c in range(sim.gw))   # full, no overlap


def test_pieces_are_connected():
    sim = _sim(seed=3)
    for pc in sim.pieces:
        assert _connected(sim, pc.cells)


def test_pieces_are_sized_in_range():
    '''Surface accretion never exceeds MAX_PIECE, and only a small tail of pieces end up under
    MIN_PIECE (boxed into a narrow well before reaching 3) — the target 3–10 dominates.'''
    small = total = 0
    for seed in range(8):
        sim = _sim(gw=30, gh=16, seed=seed)
        sizes = [len(pc.cells) for pc in sim.pieces]
        assert min(sizes) >= 1
        assert max(sizes) <= blocks.MAX_PIECE            # accretion is hard-capped at 10
        small += sum(1 for s in sizes if s < blocks.MIN_PIECE)
        total += len(sizes)
    assert small <= total * 0.08                         # <3 is a small tail (~4% in practice)


# -- contrast colouring ---------------------------------------------------

def test_neighbouring_pieces_get_different_colours():
    for seed in range(6):
        sim = _sim(gw=28, gh=16, seed=seed)
        for pid in range(len(sim.pieces)):
            for q in sim._adjacent_pieces(pid):
                assert sim.pieces[pid].color != sim.pieces[q].color   # adjacent hues contrast


# -- support graph + fill runtime ----------------------------------------

def test_generation_order_is_a_valid_drop_order():
    '''The crux: every piece rests only on the floor or on EARLIER pieces, so the support graph is
    acyclic and the generation order is itself a gravity-valid drop order (no interlocking cycles).'''
    for seed in range(8):
        sim = _sim(gw=28, gh=16, seed=seed)
        for pid, pc in enumerate(sim.pieces):
            for (r, c) in pc.cells:
                if r + 1 < sim.gh:
                    below = sim.cell_piece[r + 1][c]
                    assert below == pid or below < pid   # a supporter is always an earlier piece


def test_ground_pieces_start_releasable():
    sim = _sim()
    # at least one piece rests only on the floor (no supporters) so the fill can start immediately
    assert any(not sim.supporters[pid] for pid in range(len(sim.pieces)))
    assert any(sim._releasable(pid) for pid in range(len(sim.pieces)))


def test_progress_is_monotonic_and_clamped():
    sim = _sim()
    sim.set_progress(0.5)
    assert sim._p == 0.5
    sim.set_progress(0.2)
    assert sim._p == 0.5                                 # never rewinds
    sim.set_progress(5)
    assert sim._p == 1.0                                 # clamped


def test_fill_completes_and_never_overshoots():
    sim = _sim(gw=26, gh=15, seed=5)
    assert sim.settled_cells == 0
    sim.set_progress(1.0)
    for _ in range(2000):
        sim.step(1 / 30)
        assert sim.settled_cells <= sim.total_cells      # heap never exceeds the grid
        if sim.filled:
            break
    assert sim.filled
    assert sim.settled_cells == sim.total_cells
    assert all(pc.state == blocks.SET for pc in sim.pieces)
    # every cell of the heap is coloured in the end
    assert all(sim.settled_color[r][c] >= 0 for r in range(sim.gh) for c in range(sim.gw))


def test_fill_tracks_progress_not_racing_ahead():
    '''Settled+in-flight should stay near progress×total — the screen fills as inspection does.'''
    sim = _sim(gw=24, gh=14, seed=1)
    for p in (0.2, 0.4, 0.6, 0.8):
        sim.set_progress(p)
        for _ in range(120):
            sim.step(1 / 30)
        # once eased in, the settled heap shouldn't be far beyond what progress has "paid for" —
        # allow the anticipation lead (bridges short stalls) plus a piece of slack
        budget = (p + blocks.LEAD_FRACTION) * sim.total_cells + blocks.MAX_PIECE + 2
        assert sim.settled_cells <= budget


def test_no_piece_falls_through_a_settled_cell():
    '''Replaying to a mid-fill state, every settled cell sits on the floor or on another settled
    cell — i.e., the heap has no floating cell resting on a gap (gravity respected).'''
    sim = _sim(gw=22, gh=13, seed=4)
    sim.set_progress(0.65)
    for _ in range(300):
        sim.step(1 / 30)
    for r in range(sim.gh - 1):
        for c in range(sim.gw):
            if sim.settled_color[r][c] >= 0:
                # supported by the floor is impossible here (r < gh-1), so the cell below must be
                # filled OR belong to the same piece (which, being settled, is itself supported)
                assert sim.settled_color[r + 1][c] >= 0


def test_no_overlap_during_fall():
    '''The stagger rule's core guarantee: at every frame no screen cell is claimed twice — a falling
    piece never overlaps a settled cell or another faller (so nothing visibly passes through anything
    or lands on empty space).'''
    sim = _sim(gw=24, gh=15, seed=6)
    for i in range(900):
        sim.set_progress(min(1.0, i / 200))
        sim.step(1 / 30)
        occupied = set()
        for r in range(sim.gh):
            for c in range(sim.gw):
                if sim.settled_color[r][c] >= 0:
                    occupied.add((r, c))
        for pid in sim.falling:
            pc = sim.pieces[pid]
            drop = int(pc.offset)
            for (r, c) in pc.cells:
                rr = r - drop
                if 0 <= rr < sim.gh:                      # on-screen portion of the faller
                    assert (rr, c) not in occupied, f"overlap at {(rr, c)} frame {i}"
                    occupied.add((rr, c))
        if sim.filled:
            break
    assert sim.filled


def test_determinism_by_seed():
    a, b = _sim(seed=7), _sim(seed=7)
    assert [pc.cells for pc in a.pieces] == [pc.cells for pc in b.pieces]
    assert [pc.color for pc in a.pieces] == [pc.color for pc in b.pieces]
    c = _sim(seed=8)
    assert [pc.cells for pc in c.pieces] != [pc.cells for pc in a.pieces]


def test_only_full_blocks_are_drawn():
    '''Square blocks only: the rounded-corner style drew block-diagonal glyphs (🭁🭌🭒🭝) most
    monospace fonts lack — a fallback font per glyph made those runs choppy — so it was dropped.'''
    assert not any(n.startswith('CHAMFER') for n in dir(blocks))
    drawn = []
    s = blocks.BlocksSplash.__new__(blocks.BlocksSplash)
    s.rng = random.Random(3)
    s.w, s.h = 40, 12
    s.gw, s.gh = 20, 12
    s.sim = blocks.BlocksSim(s.gw, s.gh, s.rng)
    s._attr = list(range(blocks.PALETTE_HUES))
    s.scr = types.SimpleNamespace(erase=lambda: None)
    s._add = lambda y, x, text, attr: drawn.append(text)
    for i in range(120):
        s.render(types.SimpleNamespace(progress=min(1, i / 60), dt=1 / 30, label=None, counts=(0, 0)))
    assert drawn and set(''.join(drawn)) == {blocks.BLOCK}


def test_label_shows_counts_and_percent():
    s = blocks.BlocksSplash.__new__(blocks.BlocksSplash)
    lbl = 'checking install state'
    assert s._label_text((14, 70), lbl) == 'checking install state:   14/70 (20%)'
    assert s._label_text((70, 70), lbl) == 'checking install state:   70/70 (100%)'
    assert s._label_text((0, 0), lbl) == 'checking install state…'
