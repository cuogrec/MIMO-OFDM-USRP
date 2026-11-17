#!/usr/bin/env bash
cd "$(dirname "$0")/.." && ffplay -autoexit "$(cat runs/latest_run.txt | tr -d '\n')/video_rx.ts"
