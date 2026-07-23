FROM python:3.10-slim

WORKDIR /app

# Install dependensi dasar Linux agar library C/Rust seperti solders & curl_cffi lancar
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dan install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy seluruh file skrip & targets.txt
COPY . .

# Jalankan skrip utama
CMD ["python", "tweetv2.py"]
