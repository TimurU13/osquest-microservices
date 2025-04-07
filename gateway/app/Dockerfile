FROM python:3.9-slim

WORKDIR /app

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY . .

# Генерация gRPC файлов
RUN python -m grpc_tools.protoc -I./proto --python_out=. --grpc_python_out=. ./proto/domain_service.proto

# Команда запуска
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
