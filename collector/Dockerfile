# Use internal mirror when docker.io TLS is blocked (e.g. corp VPN).
# Mirror verified: docker-pull.emaratech.ae/python:3.12-slim
FROM docker-pull.emaratech.ae/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .

CMD ["python", "-u", "collector.py"]
