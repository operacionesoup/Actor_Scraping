# Imagen oficial de Playwright
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el proyecto
COPY . .

# Railway expone el puerto por la variable PORT
ENV PORT=8000
EXPOSE 8000

# Arranque
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]