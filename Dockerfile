FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapter.py .
COPY tests ./tests
USER 65532:65532
EXPOSE 8080
CMD ["python", "adapter.py"]
