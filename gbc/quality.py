"""Audio quality ranking shared by upgrade + dedup/albumdedup: format tier + codec-efficiency-normalised
bitrate. Quality decides which copy to keep; the metadata source (MB vs Discogs) is only a tiebreak."""
LOSSLESS = {".flac", ".alac", ".wav", ".aif", ".aiff", ".ape", ".wv", ".tta", ".dsf", ".dff"}
LOSSY = {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wma", ".mpc", ".mp2"}
# Perceptual efficiency of each lossy codec vs MP3 (=1.0): a 256k Opus beats a 320k MP3 -- compare an
# MP3-equivalent EFFECTIVE bitrate, not the raw one. Heuristic; only meaningful within the lossy tier.
_EFFICIENCY = {".opus": 1.4, ".oga": 1.3, ".ogg": 1.15, ".m4a": 1.2, ".m4b": 1.2, ".aac": 1.2,
               ".mpc": 1.1, ".mp3": 1.0, ".wma": 0.9, ".mp2": 0.7}


def rank(ext: str) -> int:
    """Format quality tier: lossless=3 > lossy=2 > unknown=1."""
    ext = ext.lower()
    return 3 if ext in LOSSLESS else 2 if ext in LOSSY else 1


def eff(ext: str, br: int) -> int:
    """Codec-efficiency-normalised bitrate (MP3-equivalent kbps) so cross-codec lossy compares fairly."""
    return round(br * _EFFICIENCY.get(ext.lower(), 1.0))
