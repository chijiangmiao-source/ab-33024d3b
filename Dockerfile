FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY tools ./tools
COPY tests ./tests
COPY verify ./verify
COPY conftest.py pytest.ini ./

RUN python tools/build_page.py

ENV APP_DB_PATH=/data/app.db
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
