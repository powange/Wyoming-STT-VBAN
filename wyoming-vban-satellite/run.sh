#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

DEBUG_LOGGING=$(bashio::config 'debug_logging')

# Count satellites
SATELLITE_COUNT=$(bashio::config 'satellites | length')
bashio::log.info "Configured ${SATELLITE_COUNT} VBAN satellite(s)"

PIDS=()

for (( i=0; i < SATELLITE_COUNT; i++ )); do
    SAT_NAME=$(bashio::config "satellites[${i}].name")
    WYOMING_PORT=$(bashio::config "satellites[${i}].wyoming_port")
    VBAN_RECEIVE_PORT=$(bashio::config "satellites[${i}].vban_receive_port")
    VBAN_RECEIVE_MODE=$(bashio::config "satellites[${i}].vban_receive_mode")
    VBAN_RECEIVE_MULTICAST_GROUP=$(bashio::config "satellites[${i}].vban_receive_multicast_group")
    VBAN_RECEIVE_STREAM_NAME=$(bashio::config "satellites[${i}].vban_receive_stream_name")
    TTS_VBAN_ENABLED=$(bashio::config "satellites[${i}].tts_vban_enabled")
    TTS_VBAN_MODE=$(bashio::config "satellites[${i}].tts_vban_mode")
    TTS_VBAN_ADDRESS=$(bashio::config "satellites[${i}].tts_vban_address")
    TTS_VBAN_PORT=$(bashio::config "satellites[${i}].tts_vban_port")
    TTS_VBAN_STREAM_NAME=$(bashio::config "satellites[${i}].tts_vban_stream_name")

    ARGS=(
        --name "${SAT_NAME}"
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

    bashio::log.info "Starting satellite '${SAT_NAME}' on Wyoming port ${WYOMING_PORT}..."
    python3 -m wyoming_vban "${ARGS[@]}" &
    PIDS+=($!)
done

bashio::log.info "All ${SATELLITE_COUNT} satellite(s) started"

# Wait for any child to exit — if one crashes, stop all
wait -n "${PIDS[@]}" 2>/dev/null
EXIT_CODE=$?

bashio::log.warning "A satellite process exited (code=${EXIT_CODE}), stopping all..."
for pid in "${PIDS[@]}"; do
    kill "${pid}" 2>/dev/null
done
wait

exit "${EXIT_CODE}"
