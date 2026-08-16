# Build stage: compile the C extensions (pycups, python-sane) into a
# virtualenv that the runtime stage copies wholesale.
FROM python:3-slim AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libcups2-dev \
        libsane-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt


FROM python:3-slim

# libsane1 brings the sane backends (including escl for driverless
# network scanners), sane-airscan adds the airscan backend. The
# libcups2 fallback covers Debian releases before the t64 rename.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libsane1 \
        sane-airscan \
    && (apt-get install -y --no-install-recommends libcups2t64 \
        || apt-get install -y --no-install-recommends libcups2) \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
COPY main.py /app/main.py

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# state.yaml (approved users, per-chat settings) is written relative
# to the working directory
RUN useradd --system --uid 1000 --create-home --home-dir /data app
WORKDIR /data
USER app

VOLUME ["/data"]

ENTRYPOINT ["python", "/app/main.py"]
CMD ["/config/config.yaml"]
