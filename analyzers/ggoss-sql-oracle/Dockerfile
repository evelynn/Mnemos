FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir oracledb==2.4.1

COPY src/ /app/src/
RUN chmod +x /app/src/ggoss_sql_oracle.py && \
    ln -s /app/src/ggoss_sql_oracle.py /usr/local/bin/ggoss-sql-oracle

RUN mkdir -p /work/.mnemos/out
WORKDIR /work
ENTRYPOINT ["ggoss-sql-oracle"]
