# PR-194 — Kotlin analyzer. Pure stdlib (regex + brace scanner), zero pip
# install — same minimal image shape as ggoss-py / ggoss-cpp / ggoss-java.
FROM python:3.12-slim

WORKDIR /opt/ggoss-kotlin
COPY src/ggoss_kotlin.py /usr/local/bin/ggoss-kotlin
RUN chmod +x /usr/local/bin/ggoss-kotlin \
 && sed -i '1i#!/usr/bin/env python3' /usr/local/bin/ggoss-kotlin

ENTRYPOINT ["/usr/local/bin/ggoss-kotlin"]
