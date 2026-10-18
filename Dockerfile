FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
RUN useradd --create-home moot && chown -R moot:moot /app
USER moot
ENV PORT=8000 MCP_USE_ANONYMIZED_TELEMETRY=false PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "moot.manufact_server", "--transport", "streamable-http", "--host", "0.0.0.0"]
