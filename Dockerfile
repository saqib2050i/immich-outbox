FROM python:3.12-slim

# Stamped by CI (see .github/workflows/publish.yml) so the running container
# can tell you which image it is.
ARG APP_VERSION=dev
ARG APP_REVISION=
ARG APP_BUILT_AT=
ENV APP_VERSION=$APP_VERSION \
    APP_REVISION=$APP_REVISION \
    APP_BUILT_AT=$APP_BUILT_AT

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

VOLUME ["/data", "/outbox", "/spool"]
EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8099/healthz',timeout=5)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
