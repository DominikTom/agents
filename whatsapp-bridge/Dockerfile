FROM node:22-slim

# git is needed by npm to fetch libsignal (a Baileys git dependency)
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json .
RUN npm install --omit=dev

COPY index.js diag.mjs ./

RUN mkdir -p /app/auth_info /app/data

# Prefer IPv4: many VPS have broken IPv6 routes to WhatsApp
ENV NODE_OPTIONS=--dns-result-order=ipv4first

EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD node -e "fetch('http://localhost:3001/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "index.js"]
