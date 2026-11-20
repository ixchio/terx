#!/usr/bin/env bash
# Render docs/assets/terx-demo.gif from a real `terx demo` run.
#
# The GIF shows the demo's actual terminal output, revealed one step at a
# time. Nothing is typed by hand: if the demo's output changes, re-run this
# script. Needs ffmpeg (with drawtext), a monospace TTF font, and Chrome.
#
#   bash docs/assets/render-demo-gif.sh [output.gif]
set -euo pipefail

output="${1:-docs/assets/terx-demo.gif}"
font="${TERX_DEMO_FONT:-}"
if [ -z "$font" ]; then
  for candidate in \
    /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf \
    /usr/share/fonts/TTF/DejaVuSansMono.ttf \
    /Library/Fonts/DejaVuSansMono.ttf \
    /System/Library/Fonts/Menlo.ttc; do
    if [ -f "$candidate" ]; then font="$candidate"; break; fi
  done
fi
test -f "$font" || { echo "No monospace font found; set TERX_DEMO_FONT" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 1. Capture the real output. A failing demo must not become a GIF.
terx demo > "$work/output.txt"

# 2. One text frame per paragraph of output, each adding to the last.
printf '$ terx demo\n' > "$work/frame00.txt"
awk -v dir="$work" '
  BEGIN { RS = ""; acc = "$ terx demo\n\n" }
  {
    acc = acc $0 "\n\n"
    file = sprintf("%s/frame%02d.txt", dir, NR)
    printf "%s", acc > file
    close(file)
  }' "$work/output.txt"

# 3. Draw each frame as terminal text on a dark background. Each line is its
#    own drawtext layer: some ffmpeg builds clip long multi-line text blocks.
width=880
height=640
line_height=23
for text in "$work"/frame*.txt; do
  filters=""
  row=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ -n "$line" ]; then
      line_file="${text%.txt}.line${row}.txt"
      printf '%s' "$line" > "$line_file"
      y=$((26 + row * line_height))
      filters="${filters}drawtext=fontfile=${font}:textfile=${line_file}:expansion=none:fontsize=16:fontcolor=0xe6edf3:x=28:y=${y},"
    fi
    row=$((row + 1))
  done < "$text"
  ffmpeg -v error -y -f lavfi -i "color=c=0x0d1117:s=${width}x${height}" \
    -vf "${filters%,}" -frames:v 1 "${text%.txt}.png"
done

# 4. Hold each step long enough to read, then loop.
durations=(0.9 1.4 3.0 3.0 3.4 3.4 6.0)
list="$work/frames.txt"
: > "$list"
index=0
last=""
for frame in "$work"/frame*.png; do
  duration="${durations[$index]:-3.0}"
  printf "file '%s'\nduration %s\n" "$frame" "$duration" >> "$list"
  last="$frame"
  index=$((index + 1))
done
printf "file '%s'\n" "$last" >> "$list"  # The concat demuxer needs the last frame twice.

ffmpeg -v error -y -f concat -safe 0 -i "$list" \
  -vf "split[a][b];[a]palettegen=max_colors=16:stats_mode=diff[p];[b][p]paletteuse=dither=none" \
  -fps_mode vfr -loop 0 "$output"

printf 'Wrote %s (%s frames) from a real `terx demo` run\n' "$output" "$index"
