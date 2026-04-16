#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

# Read options from addon config
SATELLITE_NAME=$(bashio::config 'satellite_name')
WYOMING_PORT=$(bashio::config 'wyoming_port')

VBAN_RECEIVE_PORT=$(bashio::config 'vban_receive_port')
VBAN_RECEIVE_MODE=$(bashio::config 'vban_receive_mode')
VBAN_RECEIVE_MULTICAST_GROUP=$(bashio::config 'vban_receive_multicast_group')
VBAN_RECEIVE_STREAM_NAME=$(bashio::config 'vban_receive_stream_name')

TTS_VBAN_ENABLED=$(bashio::config 'tts_vban_enabled')
TTS_VBAN_MODE=$(bashio::config 'tts_vban_mode')
TTS_VBAN_ADDRESS=$(bashio::config 'tts_vban_address')
TTS_VBAN_PORT=$(bashio::config 'tts_vban_port')
TTS_VBAN_STREAM_NAME=$(bashio::config 'tts_vban_stream_name')

DEBUG_LOGGING=$(bashio::config 'debug_logging')

# Build command-line arguments
ARGS=(
    --name "${SATELLITE_NAME}"
    --wyoming-port "${WYOMING_PORT}"
    --vban-receive-port "${VBAN_RECEIVE_PORT}"
    --vban-receive-mode "${VBAN_RECEIVE_MODE}"
)

if [ -n "${VBAN_RECEIVE_MULTICAST_GROUP}" ]; then
    ARGS+=(--vban-receive-multicast-group "${VBAN_RECEIVE_MULTICAST_GROUP}")
fi

if [ -n "${VBAN_RECEIVE_STREAM_NAME}" ]; then
    ARGS+=(--vban-receive-stream-name "${VBAN_RECEIVE_STREAM_NAME}")
fi

if [ "${TTS_VBAN_ENABLED}" = "true" ]; then
    ARGS+=(
        --tts-vban-enabled
        --tts-vban-mode "${TTS_VBAN_MODE}"
        --tts-vban-port "${TTS_VBAN_PORT}"
        --tts-vban-stream-name "${TTS_VBAN_STREAM_NAME}"
    )
    if [ -n "${TTS_VBAN_ADDRESS}" ]; then
        ARGS+=(--tts-vban-address "${TTS_VBAN_ADDRESS}")
    fi
fi

if [ "${DEBUG_LOGGING}" = "true" ]; then
    ARGS+=(--debug)
fi

bashio::log.info "Starting Wyoming VBAN Satellite..."
exec python3 -m wyoming_vban "${ARGS[@]}"
