#!/bin/sh
# Adaptive WMA -> Opus, called by the beets `convert` plugin (convert.yaml `opus` format).
# The Opus bitrate is taken from the SOURCE bitrate (clamped to [48,256] kbps) so a low-quality source
# stays low-bitrate -- it's never upscaled to look "good" (which would mask it from qa's <192k low-quality
# flag) and a good source is never downscaled. Args: $1=source $2=dest.
src="$1"
dst="$2"
br=$(ffprobe -v error -show_entries format=bit_rate -of csv=p=0 "$src" 2>/dev/null)
k=$(( ${br:-128000} / 1000 ))
[ "$k" -lt 48 ] && k=48
[ "$k" -gt 256 ] && k=256
ffmpeg -v error -i "$src" -y -vn -c:a libopus -b:a "${k}k" "$dst" || exit 1
# Validate the Opus actually decodes BEFORE returning 0: beets' keep_new moves the WMA original to quarantine
# only when this command SUCCEEDS, so a non-zero exit here keeps the original safely in place (never lose the
# only good copy to a silently-broken encode). -xerror makes a bad stream fail the decode.
if [ ! -s "$dst" ] || ! ffmpeg -v error -xerror -i "$dst" -f null - >/dev/null 2>&1; then
    rm -f "$dst"
    exit 1
fi
