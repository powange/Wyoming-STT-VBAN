ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    python3 \
    py3-pip

WORKDIR /app

COPY requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

COPY wyoming_vban/ /app/wyoming_vban/
COPY run.sh /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
