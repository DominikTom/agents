# ── 1. CSS: Tailwind with the MyBed Group OS design tokens ──────────────────
FROM node:22-slim AS css
WORKDIR /build
COPY package.json package-lock.json* tailwind.config.js ./
RUN npm install --no-audit --no-fund
COPY src/dashboard ./src/dashboard
COPY src/mcp_server/oauth.py ./src/mcp_server/oauth.py
RUN npx tailwindcss -i src/dashboard/assets/app.css -o /build/app.css --minify

# ── 2. App ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY . .
COPY --from=css /build/app.css /app/src/dashboard/static/app.css
EXPOSE 8080
CMD ["uvicorn", "src.dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
