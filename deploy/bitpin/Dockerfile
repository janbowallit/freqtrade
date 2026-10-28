# Bitpin bot image: same as the root Dockerfile, but keeps the custom FreqUI
# already in freqtrade/rpc/api_server/ui/installed instead of running install-ui.
FROM python:3.14.8-slim-trixie AS base

# Setup env
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1
ENV PATH=/home/ftuser/.local/bin:$PATH
ENV FT_APP_ENV="docker"

# Prepare environment (no sudo: the container runs without privileges)
RUN mkdir /freqtrade \
  && apt-get -o Acquire::Retries=5 update \
  && apt-get -o Acquire::Retries=5 -y install --no-install-recommends libatlas3-base curl sqlite3 libgomp1 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && useradd -u 1000 -U -m -s /bin/bash ftuser \
  && chown ftuser:ftuser /freqtrade \
  && pip install --upgrade pip

WORKDIR /freqtrade

# Install dependencies
FROM base AS python-deps
RUN  apt-get -o Acquire::Retries=5 update \
  && apt-get -o Acquire::Retries=5 -y install --no-install-recommends build-essential libssl-dev git libffi-dev libgfortran5 pkg-config cmake gcc \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --upgrade pip wheel

COPY --chown=ftuser:ftuser deploy/bitpin/requirements.lock deploy/bitpin/constraints.txt /freqtrade/
USER ftuser
RUN  pip install --user --no-cache-dir -r requirements.lock -c constraints.txt

# Copy dependencies to runtime-image
FROM base AS runtime-image

COPY --from=python-deps --chown=ftuser:ftuser /home/ftuser/.local /home/ftuser/.local

USER ftuser
COPY --chown=ftuser:ftuser . /freqtrade/

RUN pip install -e . --user --no-cache-dir --no-deps \
  && mkdir /freqtrade/user_data/ \
  && test -f freqtrade/rpc/api_server/ui/installed/index.html

HEALTHCHECK --interval=1m --timeout=10s --start-period=2m \
  CMD curl -fs http://127.0.0.1:8091/api/v1/ping || exit 1

ENTRYPOINT ["freqtrade"]
CMD [ "trade" ]
