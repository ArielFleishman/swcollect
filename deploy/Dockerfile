# Builds offline: the base image comes from "docker load" and every package from deploy/wheels.
ARG PYTHON_IMAGE=python:3.12.10-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
COPY deploy/wheels /wheels
RUN pip install --no-index --find-links /wheels -r requirements.txt && rm -rf /wheels
COPY swcollect ./swcollect
COPY catalog ./catalog
COPY config.example.yaml ./
# Written at run time by the non-root user: the discovered catalogs, verify reports, the ERD.
RUN mkdir -p /app/catalog/discovered /app/reports /app/docs     && chown -R 10001 /app/catalog/discovered /app/reports /app/docs
USER 10001
