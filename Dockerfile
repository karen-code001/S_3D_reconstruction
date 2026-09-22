FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY control_plane ./control_plane
COPY transfer_node ./transfer_node

CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]

