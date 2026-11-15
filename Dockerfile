FROM python:3.12-slim

RUN useradd --create-home app && mkdir /data && chown app /data
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -e .

USER app
ENV RECUR_AUDIT_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
