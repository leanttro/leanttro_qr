# QRCodeBrindes — Dockerfile
FROM python:3.12-slim

# Evita .pyc e deixa o log sair na hora
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependências de sistema mínimas (psycopg2 precisa de libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5002

# Produção: gunicorn. Pra debug local, troque o CMD por:
# CMD ["python", "app.py"]
CMD ["gunicorn", "--bind", "0.0.0.0:5002", "--workers", "3", "app:app"]
