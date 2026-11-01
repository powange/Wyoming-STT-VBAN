ARG BUILD_FROM
FROM ${BUILD_FROM}

# ffmpeg reads the audio of camera streams (microphone.source: rtsp)
RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY requirements.txt /app/
# A compiler only while installing: pyspeex-noise (noise suppression) has no
# prebuilt package for Alpine
RUN apk add --no-cache --virtual .build-deps build-base \
    && pip3 install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

COPY wyoming_vban/ /app/wyoming_vban/
COPY run.sh /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
