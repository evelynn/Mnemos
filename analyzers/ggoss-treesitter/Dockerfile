# PR-195 — tree-sitter multi-language analyzer. Unlike the pure-stdlib
# analyzers this one has a dependency: tree-sitter + language-pack (100+
# precompiled grammars). It is the opt-in breadth path (absorption T2).
FROM python:3.12-slim

RUN pip install --no-cache-dir "tree-sitter>=0.23" "tree-sitter-language-pack>=1.0"

WORKDIR /opt/ggoss-treesitter
COPY src/ggoss_treesitter.py /usr/local/bin/ggoss-treesitter
RUN chmod +x /usr/local/bin/ggoss-treesitter \
 && sed -i '1i#!/usr/bin/env python3' /usr/local/bin/ggoss-treesitter

ENTRYPOINT ["/usr/local/bin/ggoss-treesitter"]
