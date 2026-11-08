FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY etl ./etl
COPY ml ./ml
COPY sql ./sql
COPY tests ./tests
CMD ["python", "-m", "etl.run_all"]
