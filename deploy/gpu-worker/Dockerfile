FROM pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime@sha256:7db0e1bf4b1ac274ea09cf6358ab516f8a5c7d3d0e02311bed445f7e236a5d80 AS model_weights

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY deploy/gpu-worker/fetch-models.sh /tmp/fetch-models.sh
RUN bash /tmp/fetch-models.sh /opt/ai2-models

FROM pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime@sha256:7db0e1bf4b1ac274ea09cf6358ab516f8a5c7d3d0e02311bed445f7e236a5d80

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/opt/ai2-detectors/rslearn_projects
ENV RSLP_APPLY_WORKERS=0
ENV RSLEARN_NUM_DATA_LOADER_WORKERS=0
ENV PYTHONIOENCODING=utf-8
ENV RSLP_PREFIX=/opt/ai2-models
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev libsm6 libxext6 wget ca-certificates && rm -rf /var/lib/apt/lists/*
COPY deploy/gpu-worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
ARG RSLEARN_VERSION=0.1.17
ARG DETECTOR_REVISION=9cfaf6e9a13fcb993c687b591eb3f71df7c144c5
ENV STS_DETECTOR_REVISION=${DETECTOR_REVISION}
RUN pip install --no-cache-dir "rslearn[extra]==${RSLEARN_VERSION}"
RUN wget -q "https://codeload.github.com/allenai/rslearn_projects/tar.gz/${DETECTOR_REVISION}" -O /tmp/rslearn-projects.tar.gz && mkdir -p /opt/ai2-detectors/rslearn_projects && tar -xzf /tmp/rslearn-projects.tar.gz -C /opt/ai2-detectors/rslearn_projects --strip-components=1 && rm /tmp/rslearn-projects.tar.gz && printf 'rslearn==%s\n' "${RSLEARN_VERSION}" > /tmp/constraints.txt && pip install --no-cache-dir -c /tmp/constraints.txt -e '/opt/ai2-detectors/rslearn_projects[extra]'
COPY maritime_tip_cue /app/maritime_tip_cue
COPY config.yaml /app/config.yaml
COPY rules /app/rules
RUN mkdir -p /app/data
ARG GIT_COMMIT=
ENV STS_GIT_COMMIT=${GIT_COMMIT}
ENV STS_CONFIG_PATH=/app/config.local.yaml
COPY --from=model_weights /opt/ai2-models /opt/ai2-models
RUN python -c "import torch; import rslp.sentinel1_vessels.predict_pipeline; import rslp.sentinel2_vessels.predict_pipeline; import rslp.vessel_attribute.train; assert torch.__version__.startswith('2.7.0')"
ENTRYPOINT ["python", "-m", "maritime_tip_cue.worker"]
