FROM python:3.12-slim-trixie
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY suunto ./suunto
COPY tests ./tests
COPY VERSION .
RUN apt-get update \
    && apt-get upgrade -y \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libc6)" ge '2.41-12+deb13u5' \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' zlib1g)" ge '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir /data && chown 10001:10001 /data
EXPOSE 7383
HEALTHCHECK --interval=15s --timeout=5s --retries=4 --start-period=10s \
    CMD ["python", "-m", "suunto.container_start", "--healthcheck"]
ENTRYPOINT ["python", "-m", "suunto.container_start"]
CMD ["uvicorn", "suunto.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "7383", "--no-access-log"]
