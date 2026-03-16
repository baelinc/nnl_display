#!/bin/bash
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$1" in
    show_start|postStart) bash "$PLUGIN_DIR/fpp_plugin.sh" start ;;
    show_stop|preStop)    bash "$PLUGIN_DIR/fpp_plugin.sh" stop  ;;
esac
