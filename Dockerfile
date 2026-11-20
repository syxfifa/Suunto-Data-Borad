FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY suunto ./suunto
COPY tests ./tests
COPY VERSION .
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 7383
HEALTHCHECK --interval=15s --timeout=5s --retries=4 --start-period=10s \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:7383/healthz', timeout=3).close()"]
CMD ["uvicorn", "suunto.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "7383", "--no-access-log"]
