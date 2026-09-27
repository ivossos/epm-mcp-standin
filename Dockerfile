FROM python:3.12-slim
WORKDIR /src
COPY epm-mcp-standin.zip .
RUN python -c "import zipfile; zipfile.ZipFile('epm-mcp-standin.zip').extractall('.')" && pip install --no-cache-dir -r epm-mcp-standin/requirements.txt
# Demo simulation mode: newer server.py overrides the one inside the zip
COPY overlay/server.py epm-mcp-standin/app/server.py
WORKDIR /src/epm-mcp-standin
ENV PORT=8000
EXPOSE 8000
USER nobody
CMD ["python", "-m", "app.server"]
