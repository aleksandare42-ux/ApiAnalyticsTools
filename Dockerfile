FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/app SQL_DIR=/srv/sql
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /srv/app/
COPY sql/ /srv/sql/
COPY tests/ /srv/tests/
EXPOSE 8000 8001 8002 8003 8080
CMD ["python", "-m", "opanalytics.run", "dashboard", "--host", "0.0.0.0"]
