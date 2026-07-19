#!/bin/bash
# Playback sanity checks that don't need a GUI.
# Checks exactly the properties that caused real failures for us today:
#   1. decode integrity (no bitstream errors)
#   2. moov (index) at the FRONT   -> fast open, esp. over USB
#   3. both dimensions <= 4096     -> hardware decode path, not CPU fallback
#   4. constant 24fps frame count  -> no VFR-stacking choppiness
#   5. size ~= indexed bytes       -> no mdat bloat
#
# Deliberately NOT using qlmanage/mdls as a "will QuickTime play it?" proxy:
# tested, and qlmanage happily thumbnailed the 2896x4690 HEVC Level 6 file that
# QuickTime refused to open. It uses a different decode path and has no
# discriminating power. Real playback still needs a human in the UI.
F="$1"
FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg

# key=value parsing -- ffprobe's csv output returns fields in ITS order, not the
# order you asked for, which silently scrambles positional reads.
kv() { ffprobe -v error -select_streams v:0 -show_entries "stream=$1" \
        -of "default=noprint_wrappers=1:nokey=1" "$F" 2>/dev/null | head -1; }

W=$(kv width); H=$(kv height); NBF=$(kv nb_frames); CODEC=$(kv codec_name); LEVEL=$(kv level)
DUR=$(ffprobe -v error -show_entries format=duration -of "default=noprint_wrappers=1:nokey=1" "$F")
SZ=$(stat -f %z "$F")

echo "=============================================================="
echo "$(basename "$F")"
echo "  $CODEC ${W}x${H} level=$LEVEL frames=$NBF dur=${DUR}s size=$SZ"
echo "=============================================================="

# 1. decode integrity. Exclude "non monotonically increasing dts to muxer":
#    that's the null MUXER complaining about duplicate timestamps inherited from
#    Tesla's VFR source clips, not a decode failure. Present since the originals.
BENIGN='non monotonically increasing dts|Last message repeated'
ERRS=$("$FFMPEG" -v error -i "$F" -f null - 2>&1 | grep -vE "$BENIGN" | grep -c . )
if [ "$ERRS" = "0" ]; then echo "  [PASS] decode: no bitstream errors"
else echo "  [FAIL] decode: $ERRS error lines"
     "$FFMPEG" -v error -i "$F" -f null - 2>&1 | grep -vE "$BENIGN" | head -3
fi

# 2. moov before mdat
ORDER=$(xxd -l 400000 "$F" | grep -o -m2 -E 'moov|mdat' | head -2 | tr '\n' ' ')
case "$ORDER" in
  moov*) echo "  [PASS] moov before mdat (fast open)" ;;
  *)     echo "  [FAIL] moov/mdat order: $ORDER (slow open over USB)" ;;
esac

# 3. hardware decode envelope
if [ "$W" -le 4096 ] && [ "$H" -le 4096 ]; then
  echo "  [PASS] ${W}x${H} within the 4096 hardware-decode limit"
else
  echo "  [WARN] ${W}x${H} exceeds 4096 -> software decode, expect stutter"
fi

# 4. CFR check (grid outputs only; source concats are legitimately VFR)
if [ "$NBF" != "N/A" ] && [ -n "$NBF" ]; then
  EXP=$(awk -v d="$DUR" 'BEGIN{printf "%d", d*24}')
  DELTA=$(( NBF > EXP ? NBF - EXP : EXP - NBF ))
  if [ "$DELTA" -le 5 ]; then
    echo "  [PASS] $NBF frames ~= $EXP expected at 24fps (no VFR bloat)"
  else
    echo "  [INFO] $NBF frames vs $EXP at 24fps -- expected for VFR source concats;"
    echo "         only the grid output is normalized to CFR."
  fi
fi

# 5. mdat bloat
IDX=$(ffprobe -v error -select_streams v:0 -show_entries packet=size -of csv=p=0 "$F" | awk '{s+=$1} END {print s}')
awk -v sz="$SZ" -v idx="$IDX" 'BEGIN{r=sz/idx; printf "  [%s] container ratio %.2f (size %d / indexed %d)\n", (r<1.15?"PASS":"FAIL"), r, sz, idx}'
