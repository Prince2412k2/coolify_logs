FROM python:3.11-slim AS tailwind

WORKDIR /tw

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY tailwind.config.js ./tailwind.config.js
COPY app/templates/ ./app/templates/
COPY app/static/ ./app/static/

# Build a production Tailwind CSS bundle (no CDN runtime).
RUN curl -fsSL -o /usr/local/bin/tailwindcss \
  https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.14/tailwindcss-linux-x64 \
  && chmod +x /usr/local/bin/tailwindcss \
  && /usr/local/bin/tailwindcss -c ./tailwind.config.js -i ./app/static/input.css -o ./app/static/tailwind.css --minify

FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY --from=tailwind /tw/app/static/tailwind.css ./app/static/tailwind.css
EXPOSE 8080
VOLUME ["/data"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
