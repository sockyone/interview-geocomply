FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY checkout ./checkout
COPY fixtures ./fixtures
USER 10001:10001
EXPOSE 8080
# Keep idle client connections longer than clients pause (gunicorn defaults to 2 s, which races a
# 2 s think time and drops reused connections). Load balancers usually idle for 60 s.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--keep-alive", "30", "--access-logfile", "-", "checkout.api:app"]
