# Intel vs Apple Silicon: what a full render actually costs

Measured, not estimated. Three identical 5-minute renders were run on each of
two machines from the same commit (`7d1a25f`), the same six-camera source
clips, and the same flag set:

```
--quality high --map --gauge --fsd-scoreboard --fsd-friction-circle --fsd-note-highway
```

`--quality high` forces the software libx264 path on both machines
(`encoder_args`), so nothing here is decided by VideoToolbox — this is a
CPU-to-CPU comparison by construction.

**Summary:** the 2018 Intel Mac mini is **~2.6× slower** than the M4 on every
CPU-bound step, near-uniformly. The M4 renders this flag set at ~0.58×
realtime; the Intel mini at ~0.25×, i.e. **about 4× the footage length in wall
clock**. Three steps — gauge overlay, FSD overlay, grid encode — are ~97% of
the runtime on the Intel box, split almost evenly in thirds. The route-map step
is the one thing that does *not* scale with CPU: it is network-bound, and it
dominated the variance between windows on the Apple Silicon run.

---

## The two machines

| | Apple Silicon (baseline) | Intel (target) |
|---|---|---|
| Model | Mac mini 2024 (`Mac16,10`) | Mac mini 2018 (`Macmini8,1`) |
| CPU | Apple M4, 10 cores (4P + 6E) | Intel Core i5-8500B, 6 cores @ 3.0 GHz |
| RAM | 16 GB | 32 GB |
| macOS | 26.6.2 (25G83) | 15.7.9 (24G830) |
| Homebrew prefix | `/opt/homebrew` | `/usr/local` |
| ffmpeg | ffmpeg-full 9.0.1 | ffmpeg-full 9.0.1 |
| venv Python | 3.12.14 | 3.12.14 |
| gopro-overlay | 0.134.0 | 0.134.0 |
| `python3` (driver) | 3.9.6 (Xcode CLT) | 3.9.6 (Xcode CLT) |

Note the RAM asymmetry runs *against* the Intel box's disadvantage — it has
twice the memory and still loses by 2.6×. Nothing here is memory-bound.

**Toolchain parity was deliberate, and it took work.** A first venv built on
`python3.10` (the "known-good floor") silently resolved gopro-overlay to
**0.128.0**, because 0.129.0+ require Python ≥3.11 — a six-release gap against
the baseline's 0.134.0, sitting directly inside the gauge/FSD render loop being
measured. The venv was rebuilt on a `brew`-installed `python@3.12`, which
bottles at exactly 3.12.14, the baseline's own version. Final `pip freeze`
diff between the two machines: **one package**, `platformdirs` 4.11.3 vs
4.11.5, which is a cache-directory helper and touches nothing timed here.

---

## Per-sample, per-step

Each cell is `Apple Silicon / Intel (slowdown)`. All three windows are 5m00s of
footage from the same drive, rendered to the same 3378×1876 canvas.

| step | start (15:06:23) | middle (16:02:55) | end (17:05:26) |
|---|---|---|---|
| concat (6 cameras) | 3s / 4s (1.33×) | 3s / 4s (1.33×) | 4s / 5s (1.25×) |
| GPS extract | 2s / 5s (2.50×) | 2s / 4s (2.00×) | 2s / 4s (2.00×) |
| route map † | 1m15s / 24s | 1m25s / 14s | 6m24s / 15s |
| gauge overlay | 2m27s / 5m56s (2.42×) | 2m22s / 6m17s (2.65×) | 2m33s / 6m33s (2.57×) |
| FSD overlay ‡ | 2m23s / 6m05s (2.55×) | 2m25s / 6m39s (2.75×) | 2m48s / 6m50s (2.44×) |
| grid encode | 2m14s / 6m09s (2.75×) | 2m31s / 6m55s (2.75×) | 3m12s / 7m01s (2.19×) |
| **total** | **8m28s / 18m52s (2.23×)** | **8m50s / 20m21s (2.30×)** | **15m06s / 20m57s (1.39×)** |
| realtime multiple | 0.59× / 0.26× | 0.57× / 0.25× | 0.33× / 0.24× |
| grid encode realtime | 2.24× / 0.81× | 1.99× / 0.72× | 1.55× / 0.71× |

† Not a CPU comparison — see "The route map is not a CPU step" below. The two
columns were measured under different cache conditions on purpose.
‡ One consolidated pass carrying `scoreboard+friction-circle+note-highway`.

**With the route map excluded, the slowdown is strikingly consistent:**

| window | AS (total − map) | Intel (total − map) | slowdown |
|---|---|---|---|
| start | 7m13s | 18m28s | 2.56× |
| middle | 7m25s | 20m07s | 2.71× |
| end | 8m42s | 20m42s | 2.38× |

Mean **2.56×**, range 2.38–2.71×. There is no step where the Intel box does
disproportionately badly and none where it catches up — it is simply a
uniformly slower CPU for this workload. That uniformity is itself the useful
result: a single scalar predicts the whole pipeline.

The only sub-1.5× rows are `concat` (a `-c copy` remux — I/O, not compute, and
over in seconds either way) and `GPS extract` (2s vs 4-5s; too short to read
much into, though it is real work — SEI protobuf decoding in pure Python).

---

## Where the time actually goes

Share of each run's total:

| | gauge | FSD | grid | map | concat+GPS |
|---|---|---|---|---|---|
| AS start | 28.9% | 28.1% | 26.4% | 14.8% | 1.0% |
| AS middle | 26.8% | 27.4% | 28.5% | 16.0% | 0.9% |
| AS end | 16.9% | 18.5% | 21.2% | **42.4%** | 0.7% |
| Intel start | 31.4% | 32.2% | 32.6% | 2.1% | 0.8% |
| Intel middle | 30.9% | 32.7% | 34.0% | 1.1% | 0.7% |
| Intel end | 31.3% | 32.6% | 33.5% | 1.2% | 0.7% |

On the Intel machine the picture is almost boringly stable: **three passes,
roughly a third each, ~97% of the run.** That is the direct consequence of the
architecture described in `CLAUDE.md` — each is a full decode → draw → re-encode
generation over the hero tile or the whole canvas:

1. **gauge overlay** — `gopro-dashboard.py` composites the dial/compass/speed/
   chart panel onto the hero tile (its own internal `[0:v][1:v]overlay` +
   re-encode).
2. **FSD overlay** — one consolidated `tesla_fsd_overlay.py` pass drawing all
   three widgets, then re-encoding the hero tile again.
3. **grid encode** — the `filter_complex` that scales, stacks, labels and
   clock-stamps six cameras plus the map tile into the 3378×1876 canvas.

The `fsd-consolidated-overlay-pass` work is doing real good here: all three
`--fsd-*` flags cost **one** pass, not three. Had they still been sequential,
these Intel runs would have carried two extra ~6-minute generations each — call
it +12 minutes per 5-minute sample, roughly a 60% longer run.

**Practical lever:** dropping `--gauge` removes ~31% of the Intel runtime;
dropping all `--fsd-*` removes ~33%. They are separate subprocesses by design
(`gopro-dashboard.py` vs `tesla_fsd_overlay.py`), so they can't be merged
further without merging the tools.

---

## The route map is not a CPU step {#route-map}

This is the one row in the table that must not be read as a machine
comparison, and it is worth being explicit about why.

The Apple Silicon baseline ran against a **partly cold** OSM tile cache: its
`end` window needed ~1,845 tiles and spent **6m24s** on the map step —
**42% of that entire run** — almost all of it downloading at gopro-overlay's
~6 tiles/sec. That single step is why the `end` sample's total (15m06s) is
nearly double the other two on the same machine, and why its apparent
"slowdown" against Intel collapses to 1.39×: the Apple Silicon side was sitting
on the network, not the CPU.

For the Intel runs the baseline's 320 MB `~/.gopro-graphics/tilecache.sqlite`
was copied over first (verified with `pragma integrity_check`), so all three
windows rendered fully cache-warm. The resulting 14–24s figures are therefore
the map step's **pure CPU cost** — and they show it is nearly free: rendering a
240×156 tile is trivial work. The 24s on the first sample vs 14–15s afterwards
is the one-time cost of opening a 320 MB sqlite cache.

Two conclusions:

- **Tile fetching is machine-independent.** It is HTTP latency at a fixed ~6/s.
  An Intel Mac mini and an M4 will wait exactly as long. This is the same
  finding `map-zoom-findings.md` reached from a different direction, and the
  auto-zoom that now exists (`zoom N (auto)`) is what keeps it bounded.
- **Therefore the CPU comparison is only honest on gauge/FSD/grid**, which is
  what the 2.56× figure above is built from.

The auto-derived zoom was **identical on both machines** for all three windows,
confirming it is a deterministic function of the route, not of the hardware:

| window | route span | zoom | tiles |
|---|---|---|---|
| start | 3.7 × 0.6 km | 19 (auto) | ~649 |
| middle | 3.3 × 1.3 km | 19 (auto) | ~1,166 |
| end | 2.9 × 2.6 km | 19 (auto) | ~1,845 |

---

## Output equivalence

The Intel renders are not merely "valid" — they are the *same render*. Frame
counts and durations match the Apple Silicon outputs exactly:

| window | frames | duration | AS size | Intel size | delta |
|---|---|---|---|---|---|
| start | 7188 | 299.500s | 1,066,407,259 | 1,062,734,717 | −0.34% |
| middle | 7200 | 300.000s | 1,465,970,844 | 1,464,499,858 | −0.10% |
| end | 7179 | 299.125s | 1,245,814,949 | 1,244,047,751 | −0.14% |

Both sides produced h264 3378×1876 level=51, and the extracted GPX carried an
identical point count per window (10,775 / 10,805 / 10,775), confirming the SEI
decode is deterministic across architectures.

The sub-half-percent size differences are expected: libx264 at `-preset
veryfast` makes frame-parallel slice decisions that depend on thread count, and
the machines have 10 vs 6 cores. Same encoder, same CRF, equivalent output —
just not byte-identical.

**Integrity checks passed 5/5 on all six outputs** (decode integrity, moov
before mdat, within the 4096 hardware-decode envelope, 24fps CFR frame count,
container ratio 1.00). Method differed by machine and this matters — see the
`playcheck.sh` finding below:

- **Apple Silicon:** `./playcheck.sh` from the repo, unmodified.
- **Intel:** a copy of `playcheck.sh` placed *outside* the repo with exactly one
  line changed (the hardcoded ffmpeg path). Verified with `diff` to differ in
  that single line, so the five checks performed are otherwise identical.

---

## Predicting a full-length render

The realtime multiple holds steady across all three windows on each machine
(0.24–0.26× Intel, 0.57–0.59× Apple Silicon when the map is cheap), so it
extrapolates linearly. Rules of thumb:

- **Intel Mac mini 2018: budget ~4× the footage length.**
- **M4 Mac mini: budget ~1.7× the footage length.**

| footage | M4 | Intel mini |
|---|---|---|
| 30 min | ~52 min | ~2h 00m |
| 1 h | ~1h 43m | ~4h 00m |
| 2 h | ~3h 26m | ~8h 00m |
| 3 h | ~5h 10m | ~12h 00m |

**Both columns exclude OSM tile fetching**, which must be budgeted separately
and is the same on either machine. For a long drive this is not a rounding
error: `map-zoom-findings.md` measured one 92-minute drive wanting ~170,000
tiles at z19 — 7.7 hours of downloading. Auto-zoom exists to prevent exactly
that, but the residual fetch is still additive wall-clock that no CPU upgrade
touches. Check the `route spans … | zoom N (auto) | ~N OSM tiles | ~Ns to
fetch` line the run prints before the map step and add it.

Caveats worth stating: these are 5-minute windows, so per-run fixed costs are
amortised over less footage than a long render would amortise them over (the
extrapolation is therefore slightly pessimistic). Encode time also tracks scene
complexity — the `end` window's grid encode was ~20% slower than `start`'s on
*both* machines, from content alone. Treat ±15% as the honest band.

---

## Portability findings

The renderer itself ran on Intel **without a single source change**. The
findings below are about the surrounding tooling and docs.

### 1. `find_ffmpeg()` already handles Intel — `CLAUDE.md` understates it

`CLAUDE.md` says the script "auto-detects `/opt/homebrew/opt/ffmpeg-full/bin/
ffmpeg` and falls back to PATH". That was true once; the code at `7d1a25f` is
better than its own documentation. `tesla_combine.py:857-864` tries four
candidates — the Apple Silicon path, **the Intel path
`/usr/local/opt/ffmpeg-full/bin/ffmpeg`**, `which ffmpeg-full`, and
`which ffmpeg` — and critically **probes each for `drawtext`** rather than
trusting the first hit, keeping the first *working* binary and only falling
back to a drawtext-less one with a warning.

Verified on the Intel box: `find_ffmpeg()` returned
`('/usr/local/opt/ffmpeg-full/bin/ffmpeg', True)`, and `ffprobe` resolved as
its sibling. No PATH manipulation was needed. Worth correcting in `CLAUDE.md`.

### 2. `playcheck.sh` is genuinely broken on Intel — and fails *dishonestly*

`playcheck.sh:15` hardcodes:

```sh
FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
```

with **no fallback and no existence check** — unlike `tesla_combine.py`, which
solved this same problem properly. `/opt/homebrew` does not exist on Intel
Homebrew and cannot be created without `sudo`.

The failure mode is the bad part. It does not error out; it reports a **false
failure**:

```
  [FAIL] decode: 1 error lines
playcheck.sh: line 38: /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg: No such file or directory
```

The "1 error line" *is* the shell's own "No such file or directory" message,
counted by the `grep -c .` on line 35 as though it were a bitstream error. A
perfectly good file is reported as failing decode integrity. Running it from a
non-interactive shell (where `/usr/local/bin` is off PATH, so the bare
`ffprobe` calls on lines 19/23/68 also miss) compounds this into a cascade of
garbage: an empty `x` resolution, `[WARN] x exceeds 4096 -> software decode`,
and `awk: division by zero`.

Per instructions no repo file was modified. The fix is small and obvious
though: reuse the candidate-list-and-probe approach `find_ffmpeg()` already
implements, and resolve `ffprobe` as a sibling of the chosen ffmpeg rather than
from PATH.

### 3. `tesla_gps.py` has the same Apple-Silicon-first path, but benignly

`tesla_gps.py:92` prefers `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` and falls
back to `shutil.which("ffmpeg")`. This is only reachable when `tesla_gps.py`
runs **standalone** (e.g. `--probe`) — `tesla_combine.py` passes its own
resolved binary into `tesla_gps.extract_samples(...)`, so the combine path is
unaffected. It also does not check for `drawtext`, but GPS extraction does not
need it. Cosmetic; worth aligning with `find_ffmpeg()` eventually.

### 4. `ffmpeg-full` moved into homebrew-core — the tap instructions are stale

`CLAUDE.md` (and the README) say to get drawtext via
`brew tap homebrew-ffmpeg/ffmpeg && brew install ffmpeg-full`. **That tap does
not provide an `ffmpeg-full` formula at all** — tapping it and asking for
`ffmpeg-full` fails outright:

```
Warning: No available formula or cask with the name "homebrew-ffmpeg/ffmpeg/ffmpeg-full".
Did you mean homebrew-ffmpeg/ffmpeg/ffmpeg?
```

The tap ships a single `ffmpeg.rb` with ~40 `--with-*` options — a source
build. `ffmpeg-full` is now a **homebrew-core** formula, and it is **bottled**:

```
brew install ffmpeg-full     # 41 seconds, no tap, no compile
```

This is the single most valuable practical finding here: the setup instructions
imply a potentially multi-hour source build, and the real answer on both
architectures is a 41-second bottle pour from core. The Apple Silicon machine's
own `ffmpeg-full` also comes from core (`Homebrew/homebrew-core`), confirming
this is not Intel-specific.

### 5. The plain `ffmpeg` formula still lacks drawtext — that claim holds

Worth checking rather than assuming, since it drives the whole ffmpeg-full
requirement. Homebrew core's plain `ffmpeg` 9.0.1 bottle on Intel lists **no
`drawtext` anywhere in its `-filters` output**, and carries none of
`libfreetype`/`libfontconfig`/`libharfbuzz` in its configure line.
`ffmpeg-full` has all three. `CLAUDE.md` is correct here, on Intel as well
as Apple Silicon.

### 6. No architecture assumptions in the Python

No `sys.platform`, `platform.machine()`, `uname`, or `arm64`/`x86_64` checks
exist anywhere in `tesla_combine.py`, `tesla_gps.py`, `tesla_fsd_overlay.py`, or
`tesla_fsd_metrics.py`. Font discovery (`FONT_CANDIDATES`,
`MAP_FONT_CANDIDATES`) uses macOS system paths present on both machines —
`Menlo.ttc` and `Supplemental/Arial.ttf` resolved identically.

### 7. VideoToolbox is available on the 2018 mini too

Not exercised here (`--quality high` forces libx264), but worth recording:
`h264_videotoolbox`, `hevc_videotoolbox` and `prores_videotoolbox` are all
present on the Intel box — its T2 chip provides the encoder. A default
`--quality fast` run would take the hardware path there as well, so the 2.6×
software figure is the *worst* case, not the typical one.

### 8. The no-spaces venv gotcha is real and was avoided

Per `CLAUDE.md`, the repo was rsynced to `/Users/reza/teslacam-studio` (no
spaces). The code sidesteps the broken console-script shebang anyway by
invoking `./.venv/bin/python ./.venv/bin/gopro-dashboard.py`, which was
confirmed in the Intel dry-run output.

---

## Method

- Both machines rendered from commit `7d1a25f`, working tree clean (verified by
  `git log -1` / `git status` on each). Confirmed after the fact as well: the
  Intel copy of `tesla_combine.py` hashes identically to the committed
  `7d1a25f` blob, and the Apple Silicon working copy was only modified later
  the same day (by unrelated concurrent work on another branch), well after
  both sets of renders had finished.
- Source clips selected by mirroring `select_clips()`'s own rule (a clip
  belongs to a window when the *next* clip's start is after the window start and
  its own start is before the window end), which correctly pulls in the clip
  *containing* `17:05:26` for the `end` window. 17 timestamps × 6 cameras = 102
  clips copied; **byte-size verified identical** on both sides, and each camera
  confirmed to hold the same 17 clips. Both machines independently reported
  `Trim window selects 5 of N clips per camera` for every window.
- Each sample used its own `--output-dir` so concat caches could not collide
  across trim windows; every run reported `6 built, 0 reused`.
- Intel runs were sequential under plain `nohup` (`setsid` does not exist on
  macOS), one render at a time, no other load on the box.
- Apple Silicon timings were read from the baseline log produced before this
  machine took on other work; no Apple Silicon number here was re-measured
  under contention.
- Intel benchmark scratch (renders and the copied clips, ~15.5 GB) was removed
  afterwards. The source footage was never touched — only copies were made.
