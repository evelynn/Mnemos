# PR-115 — Python analyzer. Pure stdlib (ast). Zero pip install
# means the smallest, fastest, most reproducible analyzer image
# of the six. Operator-friendly per §2.10.
FROM python:3.12-slim

WORKDIR /opt/ggoss-py
COPY src/ggoss_py.py /usr/local/bin/ggoss-py
RUN chmod +x /usr/local/bin/ggoss-py \
 && sed -i '1i#!/usr/bin/env python3' /usr/local/bin/ggoss-py

# Match the platform's invocation expectation in
# analyzers/runner.py: `<binary> <verb> <target>`.
ENTRYPOINT ["/usr/local/bin/ggoss-py"]
