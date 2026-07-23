FROM python:3.10-slim

WORKDIR /app

# Install pip wheel versi terbaru agar tidak perlu compile dari source
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy requirements dan install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy seluruh source code
COPY . .

# Jalankan script
CMD ["python", "tweetv2.py"]
