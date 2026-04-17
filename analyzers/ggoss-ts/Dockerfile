FROM node:20-alpine

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev

COPY src/ ./src/
RUN chmod +x ./src/index.mjs && ln -s /app/src/index.mjs /usr/local/bin/ggoss-ts

RUN mkdir -p /work/.mnemos/out
WORKDIR /work

ENTRYPOINT ["ggoss-ts"]
