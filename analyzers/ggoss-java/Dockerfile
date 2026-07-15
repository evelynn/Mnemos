# PR-192 — Java analyzer. Pure stdlib (regex + brace scanner), zero
# pip install — same minimal image shape as ggoss-py / ggoss-cpp.
# Operator-friendly per §2.10.
FROM python:3.12-slim

WORKDIR /opt/ggoss-java
COPY src/ggoss_java.py /usr/local/bin/ggoss-java
RUN chmod +x /usr/local/bin/ggoss-java \
 && sed -i '1i#!/usr/bin/env python3' /usr/local/bin/ggoss-java

# Match the platform's invocation expectation in
# analyzers/runner.py: `<binary> <verb> <target>`.
ENTRYPOINT ["/usr/local/bin/ggoss-java"]
