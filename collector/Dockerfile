FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /srv

COPY collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/*.py ./
COPY collector/app ./app

RUN useradd --system --uid 10001 collector
USER collector

CMD ["python", "-m", "app.main"]
