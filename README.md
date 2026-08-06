# configsys-splash-blocks — the "blocks" startup splash

A [configsys](https://github.com/spacemeat/configsys) **splash** code plugin: while configsys
inspects install state, coloured blocks fall from the top and stack up, completing rows along the
bottom and filling the screen. It's Tetris-ish but freed of the four-cell limit — shapes are **3–10
blocks** — and, unlike Tetris, **no rows are ever cleared**; the heap only grows. Shapes stream in
from just off the top of the screen, several at once, until it's full. Each block cell is **two
ascii blocks side-by-side** (≈ a square on a modern terminal), and each polyomino is **one solid
colour** (picked for hue contrast with its neighbours), so the settled screen reads as distinct
shapes stacked. Each run flips a coin for its corner style: **all pieces chamfered** (softly beveled
outer corners, via the filled block-diagonals 🭁🭌🭒🭝) or **all square**.

The fill is **driven by progress**: it finishes filling just as inspection completes (0 → 100%).
Purely cosmetic — the blocks are paced by configsys's real progress, never the other way.

It's a sibling of [configsys-splash-ocean](https://github.com/spacemeat/configsys-splash-ocean) —
both ride the same **splash provider ABI**, so a splash is just a trusted code plugin you select
with the `splash:` machine setting. Core ships only a trust-free `braille-bar` default.

## How it fills so cleanly (and on time)

The screen is a grid of cells (`W/2 × H`). Before anything falls, the sim **solves a complete
tiling** of that grid into polyominoes by *surface accretion*: it always starts the next piece at
the surface of the currently-lowest column and only ever adds a cell that has support directly
beneath it. Two properties fall out, and both matter:

- **No holes** — we always fill the lowest gap and never cover empty space, so the board packs
  bottom-up with nothing trapped.
- **No support cycles** — every piece rests on the floor or on *earlier* pieces, so the generation
  order is itself a valid gravity drop order. (An arbitrary tiling doesn't have this: pieces meeting
  along a jagged border mutually support each other, and the cyclic support graph has no clean drop
  order — the fill deadlocks or crawls.)

Replay then drops each piece into its solved slot. Every piece spawns just off the top and falls
the whole way at the same speed, so a piece is released only once each of its supporters has
descended past its landing zone (`offset ≤ our own spawn offset`) — same-speed dead reckoning that
guarantees a piece never catches or lands before its support, while letting pieces **stream from the
top** instead of waiting for the support to fully settle first. Knowing the exact cell count, it
paces releases so the screen finishes filling right as inspection does. Neighbouring pieces are
given **contrasting hues** by greedy graph-colouring over a fixed palette.

Spawns are **staggered**: the sim reacts to an eased progress and caps how many cells may spawn per
frame, so the stepwise nature of real progress (several fast checks landing at once, or a whole
support layer clearing) becomes a steady stream instead of a burst. A small anticipation *lead*
spawns slightly ahead of progress to bridge short stalls between checks — but a genuinely long stall
on one slow check (a network/package query where progress just doesn't move) will still show a
pause; that timing can't be predicted from partial progress on variadic checks.

## The shape (a splash code plugin)

| File | Role |
| --- | --- |
| `plugin.hu` | manifest — `name`, `requires-abi`, `provides.splashes`, `code:` |
| `blocks.py` | the provider: `BlocksSim` (curses-free, deterministic, unit-tested) + `BlocksSplash(Splash)` + `SPLASHES = [BlocksSplash]` |
| `test/` | the solver / colouring / fill-runtime unit tests |

The contract (see `configsys/splashes.py`): subclass `Splash`, set a class-level `name`, implement
`render(frame)` — the **host** (`configsys.tui.splash.run_splash`) owns the frame loop, the skip
key, the deadline, and the plain-text fallback, feeding each frame a minimal
`SplashFrame(progress, counts, label, dt, elapsed, done)`. Export `SPLASHES = [YourSplash]`.

### A note on colours

curses `color_pair()` is 8-bit and the host doesn't recycle pairs between frames, so a run must stay
under 256 distinct `(fg, bg)` pairs. There can be 150+ pieces, so they can't each own a colour;
instead a **fixed palette** (`PALETTE_HUES` hues, one pair each) is pre-baked once, and pieces are
assigned hues by contrast so neighbours differ — reusing the palette across non-adjacent pieces.

## Use it

```sh
configsys plugin add github:spacemeat/configsys-splash-blocks   # or a local path / file: source
configsys plugin trust configsys-splash-blocks                  # it runs code, so trust is required
configsys config set splash configsys-splash-blocks            # select it (or the short `blocks`)
```

`configsys config set splash off` disables the splash entirely; unset falls back to the built-in
`braille-bar` line.

## Tests

```sh
PYTHONPATH=/path/to/configsys python -m pytest test/ -q
```

Only the curses-free core (`BlocksSim`) is unit-tested; `BlocksSplash.render` drives curses and is
exercised by hand. The sim is deterministic given its `rng`, so the assertions are stable.
