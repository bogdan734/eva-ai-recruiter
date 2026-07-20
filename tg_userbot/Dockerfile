FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY persona.py store.py service.py ./
# session + sqlite db + runtime state live on a mounted volume at /data
ENV TG_STATE_PATH=/data/eva_state.json
CMD ["python", "service.py"]
