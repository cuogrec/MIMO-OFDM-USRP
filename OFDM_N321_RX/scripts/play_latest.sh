#!/usr/bin/env bash
cd "$(dirname "$0")/.." && ffplay -autoexit "$(tr -d '\n' < runs/latest_run.txt)/video_rx.ts"
