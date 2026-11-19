FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --system --uid 10001 --no-create-home app
COPY --chown=app *.py ./
RUN chown app /app
USER app

ENV PORT=8000

CMD ["python", "main.py", "serve"]
