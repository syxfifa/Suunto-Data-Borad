FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY suunto ./suunto
COPY tests ./tests
COPY VERSION .
RUN mkdir /data && chown 10001:10001 /data
EXPOSE 7383
HEALTHCHECK --interval=15s --timeout=5s --retries=4 --start-period=10s \
    CMD ["python", "-m", "suunto.container_start", "--healthcheck"]
ENTRYPOINT ["python", "-m", "suunto.container_start"]
CMD ["uvicorn", "suunto.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "7383", "--no-access-log"]
