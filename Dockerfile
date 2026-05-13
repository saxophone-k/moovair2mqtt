FROM python:3.12-slim

WORKDIR /app

# Installer les dépendances d'abord (cache Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le bridge
COPY moovair2mqtt.py .

# Utilisateur non-root (bonne pratique sécurité)
RUN useradd -r -u 1001 moovair2mqtt
USER moovair2mqtt

CMD ["python", "-u", "moovair2mqtt.py"]
