ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip restic \
    && restic version

COPY app /app
COPY rootfs /

RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt \
    && chmod a+x /etc/s6-overlay/s6-rc.d/restic_backup_api/run

LABEL io.hass.type="addon"
