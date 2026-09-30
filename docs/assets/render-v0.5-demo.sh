#!/usr/bin/env bash
# Render the 20-second product demo from real local-browser frames.
set -euo pipefail

frame_dir="${1:-/tmp/terx-v0.5-demo-frames}"
output="${2:-docs/assets/terx-v0.5-saved-tool-demo.mp4}"
font="/usr/share/fonts/truetype/lato/Lato-Medium.ttf"

for frame in 01-task-ready.png 02-task-recorded.png 03-tool-replayed.png; do
  test -s "${frame_dir}/${frame}"
done

ffmpeg -y \
  -loop 1 -t 6 -i "${frame_dir}/01-task-ready.png" \
  -loop 1 -t 7 -i "${frame_dir}/02-task-recorded.png" \
  -loop 1 -t 7 -i "${frame_dir}/03-tool-replayed.png" \
  -filter_complex "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,drawbox=x=0:y=0:w=iw:h=104:color=0x0b1020@0.92:t=fill,drawtext=fontfile=${font}:text='Agent completes a vendor order lookup':fontcolor=white:fontsize=36:x=48:y=30[v0];[1:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,drawbox=x=0:y=0:w=iw:h=104:color=0x0b1020@0.92:t=fill,drawtext=fontfile=${font}:text='TERX saves the order status tool with input fields':fontcolor=white:fontsize=32:x=48:y=34[v1];[2:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,drawbox=x=0:y=0:w=iw:h=104:color=0x0b1020@0.92:t=fill,drawtext=fontfile=${font}:text='Later call returns fresh status with new input and zero TERX model calls':fontcolor=white:fontsize=28:x=48:y=34[v2];[v0][v1][v2]concat=n=3:v=1:a=0,format=yuv420p[v]" \
  -map "[v]" -r 30 -movflags +faststart "${output}"

printf 'Wrote 20-second demo: %s\n' "${output}"
