#!/bin/sh
# Hi-res FLAC (>48 kHz and/or >16-bit) -> 16-bit / <=48 kHz FLAC, called by the beets `convert` plugin
# (convert.yaml `flac16` format). A 24/192-style master carries no audible playback benefit over CD-quality
# 16/44.1 (transparent: >22 kHz is inaudible, 96 dB already exceeds any listening range) yet costs ~6-8x the
# size + very slow tag writes. The original hi-res file is MOVED to quarantine by keep_new -- NEVER deleted.
# Family-matched target rate avoids cross-family resampling. Validates the output decodes BEFORE returning 0
# (keep_new quarantines the original only on success, so a broken encode leaves the only good copy in place).
# Args: $1=source $2=dest.
src="$1"
dst="$2"
sr=$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of csv=p=0 "$src" 2>/dev/null)
case "$sr" in
    88200|176400|352800) tsr=44100 ;;   # 44.1 kHz family
    96000|192000|384000) tsr=48000 ;;   # 48 kHz family
    *) tsr="${sr:-44100}" ;;            # already <=48k (or unreadable): keep the rate, only drop depth to 16-bit
esac
# -vn + -map_metadata -1: drop any source cover stream + container tags; beets re-tags + re-embeds art after
# keep_new. soxr precision 28 + dithered conversion to s16 = transparent down-conversion.
ffmpeg -v error -i "$src" -y -vn -map_metadata -1 \
    -af "aresample=resampler=soxr:precision=28:osr=$tsr" -sample_fmt s16 \
    -acodec flac "$dst" || exit 1
if [ ! -s "$dst" ] || ! ffmpeg -v error -xerror -i "$dst" -f null - >/dev/null 2>&1; then
    rm -f "$dst"
    exit 1
fi
