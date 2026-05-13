# vertex-trainer — runs the f7i-cdk SageMaker training scripts unmodified
# under Vertex AI by translating env/channels in entrypoint.py.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The training scripts (vendored verbatim from f7i-cdk).
COPY sagemaker_rl/ /opt/training/

# Vertex env -> SageMaker env shim.
COPY entrypoint.py /opt/entrypoint.py

ENTRYPOINT ["python", "/opt/entrypoint.py"]
