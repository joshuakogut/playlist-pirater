#!/usr/bin/env bash

# usage: playlist-pirater [-h] [--music-dir MUSIC_DIR] [--quality {320,256,192,128,96}] [--search-limit SEARCH_LIMIT] [--metadata-limit METADATA_LIMIT]
#                         [--mpd-playlist-dir MPD_PLAYLIST_DIR] [--no-mpd-playlist] [--report REPORT] [--dry-run] [--overwrite] [--retries RETRIES]
#                         [--ffmpeg-location FFMPEG_LOCATION] [--limit LIMIT] [--quiet]
#                         csv
# 

./playlist_pirater.py --music-dir ./music --mpd-playlist-dir ./playlists  example.csv


