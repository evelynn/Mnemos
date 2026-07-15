# PR-193 — Web (HTML/CSS) analyzer. Pure stdlib (regex), zero pip install
# — same minimal image shape as ggoss-py / ggoss-cpp / ggoss-java.
FROM python:3.12-slim

WORKDIR /opt/ggoss-web
COPY src/ggoss_web.py /usr/local/bin/ggoss-web
RUN chmod +x /usr/local/bin/ggoss-web \
 && sed -i '1i#!/usr/bin/env python3' /usr/local/bin/ggoss-web

ENTRYPOINT ["/usr/local/bin/ggoss-web"]
