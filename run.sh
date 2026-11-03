#!/usr/bin/env bash
# Without installing: ./run.sh            -> wallpaper in the background
#                     ./run.sh window     -> the window (gives the terminal back)
#                     ./run.sh stop       -> stops everything
#                     ./run.sh run -f     -> everything in this terminal, with logs (development)
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -m kingdom "$@"
