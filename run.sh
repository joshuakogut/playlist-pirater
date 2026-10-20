#!/usr/bin/env bash

# usage: playlist-pirater [-h] [--music-dir MUSIC_DIR]
#                         [--quality {320,256,192,128,96}]
#                         [--search-limit SEARCH_LIMIT]
#                         [--metadata-limit METADATA_LIMIT]
#                         [--m3u-playlist-dir M3U_PLAYLIST_DIR]
#                         [--no-m3u-playlist] [--absolute-paths]
#                         [--report REPORT] [--dry-run] [--overwrite]
#                         [--retries RETRIES]
#                         [--ffmpeg-location FFMPEG_LOCATION] [--limit LIMIT]
#                         [--no-tags] [--tag-only] [--quiet]
#                         csv

./playlist_pirater.py --music-dir ./music --m3u-playlist-dir ./playlists  example.csv
