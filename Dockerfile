FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

RUN useradd --system --uid 10001 --no-create-home app
USER app

ENV PORT=8000

CMD ["sh", "-c", "python main.py serve --port ${PORT}"]
