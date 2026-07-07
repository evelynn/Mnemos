# PR-191 — C/C++ analyzer. Pure stdlib (regex + brace scanner), zero
# pip install — same minimal image shape as ggoss-py. Operator-friendly
# per §2.10.
FROM python:3.12-slim

WORKDIR /opt/ggoss-cpp
COPY src/ggoss_cpp.py /usr/local/bin/ggoss-cpp
RUN chmod +x /usr/local/bin/ggoss-cpp \
 && sed -i '1i#!/usr/bin/env python3' /usr/local/bin/ggoss-cpp

# Match the platform's invocation expectation in
# analyzers/runner.py: `<binary> <verb> <target>`.
ENTRYPOINT ["/usr/local/bin/ggoss-cpp"]
