#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

# All satellites run in a single process, which reads /data/options.json
# directly: satellites sharing a VBAN receive port must share one UDP
# socket (separate sockets on one port split unicast datagrams between
# them instead of duplicating them). Defaults for missing/null options and
# config validation are handled in Python (wyoming_vban/config.py).

SATELLITE_COUNT=$(bashio::config 'satellites | length')
bashio::log.info "Configured ${SATELLITE_COUNT} VBAN satellite(s)"

exec python3 -m wyoming_vban --options /data/options.json
