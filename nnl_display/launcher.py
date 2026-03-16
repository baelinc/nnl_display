#!/usr/bin/env python3
"""
launcher.py  —  NNL FPP Plugin v1.0.0 Entry Point
===================================================
Runs the display engine and the settings web server in the same
Python process so the web server can access the shared canvas
for on-demand preview rendering without any IPC or file writing.

Thread layout:
  Main thread   → web_server  (blocks, serves HTTP on port 8180)
  Daemon thread → nnl_display (renders pixels, drives ColorLight card)
"""

import os
import sys
import threading
import logging

# Ensure the plugin directory is on the path so both modules can find config etc.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [NNL] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('nnl.launcher')

# ── Write PID so fpp_plugin.sh can kill us cleanly ────────────────────────────
PID_FILE = '/tmp/nnl_display.pid'
with open(PID_FILE, 'w') as f:
    f.write(str(os.getpid()))

# ── Start display engine in a background daemon thread ───────────────────────
def run_display():
    try:
        from nnl_display import NNLDisplay
        d = NNLDisplay()
        d.start()
    except Exception as e:
        log.error('Display engine crashed: %s', e)

display_thread = threading.Thread(target=run_display, daemon=True, name='NNLDisplay')
display_thread.start()
log.info('Display engine thread started')

# ── Run web server on the main thread (blocks until killed) ──────────────────
try:
    from web_server import run as run_web
    log.info('Starting settings UI on port 8180')
    run_web()
except KeyboardInterrupt:
    log.info('Shutting down')
except Exception as e:
    log.error('Web server error: %s', e)
