FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    git \
    curl \
    nodejs \
    npm \
 && node -e "const major=Number(process.versions.node.split('.')[0]); if (major < 20) process.exit(1)" \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/ /app
COPY docs/ /docs/
RUN pip install --upgrade pip "setuptools>=78.1.1" \
 && pip install ".[analyzers-treesitter]" \
 && python -m pip uninstall --yes pip

# The worker invokes trusted analyzers as bounded local subprocesses.  The
# former Compose profile only built disconnected images, leaving every binary
# absent from the worker and allowing a successful empty graph.  Ship the
# runnable source contracts in the image and install the sole Node dependency.
COPY analyzers/ /analyzers/
RUN cd /analyzers/ggoss-ts \
 && npm ci --omit=dev

ENV MNEMOS_INREPO_ANALYZERS=1

RUN mkdir -p /var/lib/mnemos

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
