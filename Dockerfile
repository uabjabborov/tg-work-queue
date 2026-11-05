FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py database.py presentation.py review_urls.py leaderboard.py scheduler.py ./

# Catch missing runtime modules before an image can be published.
RUN python -c "import os, tempfile; \
    smoke_data_dir = tempfile.TemporaryDirectory(); \
    os.environ['DATA_DIR'] = smoke_data_dir.name; \
    import bot; \
    smoke_data_dir.cleanup()"

CMD ["python", "bot.py"]
