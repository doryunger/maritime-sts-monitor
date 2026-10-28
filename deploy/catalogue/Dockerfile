FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY deploy/catalogue/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY maritime_tip_cue /app/maritime_tip_cue
COPY config.yaml /app/config.yaml
COPY rules /app/rules
COPY hotspot-tests/areas.json /app/hotspot-tests/areas.json
RUN mkdir -p /app/data
ENTRYPOINT ["python"]
