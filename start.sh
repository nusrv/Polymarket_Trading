#!/bin/bash
if [ "$CRON_MODE" = "1" ]; then
    python fastloop_trader.py --quiet
else
    python dashboard.py
fi
