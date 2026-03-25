#!/bin/bash
if [ "$CRON_MODE" = "1" ]; then
    python fastloop_trader.py --live --quiet
else
    python dashboard.py
fi
