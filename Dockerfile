FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY wang ./wang
RUN pip install --no-cache-dir . && useradd --create-home wang && mkdir /app/data && chown wang /app/data
USER wang
EXPOSE 8000
CMD ["uvicorn", "wang.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
