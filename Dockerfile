FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY suunto ./suunto
COPY tests ./tests
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "suunto.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
