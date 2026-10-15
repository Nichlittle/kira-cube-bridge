# Build from the kit folder:
#   docker build -f bridge/Dockerfile -t kira-cube-bridge .
FROM python:3.12-slim
WORKDIR /app
COPY bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge/server.py ./
ENV PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
