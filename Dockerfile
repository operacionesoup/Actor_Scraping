# Imagen oficial de Playwright (ya trae Chromium + dependencias)
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Instala dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el proyecto
COPY . .

# Railway expone el puerto por la variable PORT
ENV PORT=8000
EXPOSE 8000

# Arranque
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]