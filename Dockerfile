FROM python:3.11-slim

# Herramientas mínimas para que playwright install --with-deps funcione
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala Chromium + todas sus dependencias de sistema automáticamente
RUN playwright install chromium --with-deps

COPY . .

# python -m uvicorn evita problemas de PATH con el ejecutable uvicorn
CMD ["sh", "-c", "python -m uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]