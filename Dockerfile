FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
# Playwright a besoin de Chromium + de ses libs système pour le scraping
# headless (bouton "Plus" de Forebet). install-deps utilise apt-get, donc
# doit tourner avant de passer à un utilisateur non-root si vous en ajoutez
# un plus tard. Ça alourdit l'image d'environ 300 Mo.
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install-deps chromium \
    && python -m playwright install chromium
COPY . .

RUN chmod +x /app/start.sh && mkdir -p /app/cache
CMD ["./start.sh"]
