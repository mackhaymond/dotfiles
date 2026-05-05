#!/bin/bash

CPU_PERCENT=$(top -l 2 -s 0 | grep -E "^CPU" | tail -1 | awk '{ print $3 + $5"%" }')

sketchybar --set cpu label="$CPU_PERCENT"
