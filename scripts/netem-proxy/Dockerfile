FROM python:3.12-alpine

RUN apk add --no-cache iproute2
WORKDIR /opt/netem-proxy
COPY tcp_proxy.py ./tcp_proxy.py
ENTRYPOINT ["python", "tcp_proxy.py"]
