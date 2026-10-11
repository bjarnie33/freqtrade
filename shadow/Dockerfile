FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY shadow_advisor.py .
CMD ["python", "-u", "shadow_advisor.py"]
