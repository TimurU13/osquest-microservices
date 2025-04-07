import json
import logging
import grpc
import redis
import pika
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from prometheus_client import Counter, Summary, generate_latest, CONTENT_TYPE_LATEST
from pythonjsonlogger.json import JsonFormatter
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

import domain_service_pb2
import domain_service_pb2_grpc
import logstash
import os

# Логгер
logger = logging.getLogger("gateway")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
logHandler.setFormatter(JsonFormatter())
logger.addHandler(logHandler)

LOGSTASH_HOST = os.getenv("LOGSTASH_HOST", "logstash")
LOGSTASH_PORT = 5000
logstash_handler = logstash.TCPLogstashHandler(
    host=LOGSTASH_HOST,
    port=LOGSTASH_PORT,
    version=1,
    message_type='log',
    fqdn=False
)
logger.addHandler(logstash_handler)

# Tracing
trace.set_tracer_provider(
    TracerProvider(resource=Resource.create({"service.name": "gateway"}))
)
tracer = trace.get_tracer(__name__)

# Приложение FastAPI
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis
redis_client = redis.Redis(host='redis', port=6379, db=0)

# RabbitMQ
credentials = pika.PlainCredentials("admin", "admin")

def connect_to_rabbitmq():
    for i in range(30):
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host='rabbitmq', credentials=credentials)
            )
            logger.info("Connected to RabbitMQ!")
            return connection
        except pika.exceptions.AMQPConnectionError:
            logger.warning(f"Waiting for RabbitMQ... attempt {i+1}")
            time.sleep(3)
    raise Exception("Failed to connect to RabbitMQ after 30 attempts")

rabbit_connection = connect_to_rabbitmq()
rabbit_channel = rabbit_connection.channel()
rabbit_channel.queue_declare(queue='crud_operations', durable=True)

# Prometheus
REQUEST_COUNT = Counter('request_count', 'App Request Count', ['method', 'endpoint'])
REQUEST_LATENCY = Summary('request_latency_seconds', 'Request latency', ['endpoint'])

# gRPC
def get_grpc_stub():
    channel = grpc.insecure_channel('domain_service:50051')
    stub = domain_service_pb2_grpc.DomainServiceStub(channel)
    return stub

# Модели
class BlogBase(BaseModel):
    create_year: str = Field(..., example="2025")
    title: str = Field(..., example="Title")
    text: str = Field(..., example="hello, i am mr.mr.")
    author: str = Field(..., example="Mr. Mister")

class BlogCreate(BlogBase):
    pass

class BlogUpdate(BlogBase):
    id: str = Field(..., example="unique_id")

@app.middleware("http")
async def prometheus_middleware(request, call_next):
    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    with REQUEST_LATENCY.labels(endpoint=request.url.path).time():
        response = await call_next(request)
    return response

@app.get("/blogs")
def list_blogs():
    cache_key = "blogs:all"
    cache_ttl = 60
    try:
        cached_blogs = redis_client.get(cache_key)
        if cached_blogs:
            logger.info("Cache hit for blogs")
            return json.loads(cached_blogs)
        logger.info("Cache miss for blogs, querying gRPC")
        stub = get_grpc_stub()
        request = domain_service_pb2.ListRequest()
        response = stub.ListBlogs(request)
        blogs = [
            {
                "id": blog.id,
                "create_year": blog.create_year,
                "title": blog.title,
                "text": blog.text,
                "author": blog.author
            } for blog in response.blogs
        ]
        redis_client.setex(cache_key, cache_ttl, json.dumps(blogs))
        logger.info("Blogs cached successfully")
        return blogs
    except grpc.RpcError as e:
        logger.error(f"gRPC error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/blog/{item_id}")
def get_blog(item_id: str):
    cache_key = f"blog:{item_id}"
    cached = redis_client.get(cache_key)
    if cached:
        logger.info(f"Cache hit for {cache_key}")
        return json.loads(cached)
    logger.info(f"Cache miss for {cache_key}, querying domain service via gRPC")
    stub = get_grpc_stub()
    request = domain_service_pb2.GetRequest(id=item_id)
    try:
        response = stub.GetBlog(request)
        blog = json.loads(response.blog)
        redis_client.set(cache_key, json.dumps(blog), ex=60)
        return blog
    except grpc.RpcError as e:
        logger.error(f"gRPC error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

def safe_publish(message):
    global rabbit_connection, rabbit_channel
    try:
        if rabbit_channel.is_closed or rabbit_connection.is_closed:
            logger.warning("RabbitMQ channel was closed. Reconnecting...")
            rabbit_connection = connect_to_rabbitmq()
            rabbit_channel = rabbit_connection.channel()
            rabbit_channel.queue_declare(queue='crud_operations', durable=True)
        rabbit_channel.basic_publish(
            exchange='',
            routing_key='crud_operations',
            body=json.dumps(message),
            properties=pika.BasicProperties(delivery_mode=2),
        )
    except Exception as e:
        logger.error(f"Failed to send message to RabbitMQ: {e}")
        raise

@app.post("/blog")
def create_blog(blog: BlogCreate):
    message = {"operation": "create", "data": blog.dict()}
    try:
        safe_publish(message)
        logger.info(f"Sent create operation to RabbitMQ: {message}")
        return {"status": "success", "operation": "create"}
    except Exception:
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.put("/blog")
def update_blog(blog: BlogUpdate):
    message = {"operation": "update", "data": blog.dict()}
    try:
        safe_publish(message)
        logger.info(f"Sent update operation to RabbitMQ: {message}")
        return {"status": "success", "operation": "update"}
    except Exception:
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.delete("/blog/{item_id}")
def delete_blog(item_id: str):
    message = {"operation": "delete", "data": {"id": item_id}}
    try:
        safe_publish(message)
        logger.info(f"Sent delete operation to RabbitMQ: {message}")
        return {"status": "success", "operation": "delete"}
    except Exception:
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
