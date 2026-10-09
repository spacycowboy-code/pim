FROM python:3.12-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gcc python3-dev libkrb5-dev krb5-user \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-linux.txt /app/
RUN pip install --no-cache-dir -r requirements-linux.txt
RUN useradd --create-home --uid 10001 pim
COPY . /app
USER pim
ENV PIM_MI_AUTH=kerberos PIM_ALLOW_RTD_REFRESH=0
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1
CMD ["python", "linux_server.py"]
