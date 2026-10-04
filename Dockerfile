FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home editor
WORKDIR /app
COPY pyproject.toml ./
COPY movie_editor ./movie_editor
RUN pip install --no-cache-dir . && mkdir data && chown -R editor:editor /app
USER editor
CMD ["python", "-m", "movie_editor"]
