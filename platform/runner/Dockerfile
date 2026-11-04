FROM python:3.13-slim
WORKDIR /suite
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY suite_runtime ./suite_runtime
RUN mkdir /results && chown 10001:10001 /results
USER 10001:10001
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "suite_runtime"]
