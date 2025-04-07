import json
import logging
import grpc
from concurrent import futures
import time
import pika
from pymongo import MongoClient
from bson import ObjectId
import domain_service_pb2
import domain_service_pb2_grpc

import logstash


logger = logging.getLogger("domain_service")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logHandler.setFormatter(formatter)
logger.addHandler(logHandler)

import os
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

# Настройка MongoDB
mongo_client = MongoClient('mongodb://mongo:27017/')
db = mongo_client['blogs_db']
collection = db['blogs']

# Настройка RabbitMQ
credentials = pika.PlainCredentials("admin", "admin")
rabbit_connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq', credentials=credentials))
rabbit_channel = rabbit_connection.channel()
rabbit_channel.queue_declare(queue='crud_operations', durable=True)

class DomainServiceServicer(domain_service_pb2_grpc.DomainServiceServicer):
    def GetBlog(self, request, context):
        try:
            obj_id = ObjectId(request.id)
        except Exception as e:
            logger.error(f"Invalid ID format: {request.id}")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('Invalid ID format')
            return domain_service_pb2.GetResponse()

        blog = collection.find_one({"_id": obj_id})
        if blog:
            blog['id'] = str(blog['_id'])
            del blog['_id']
            return domain_service_pb2.GetResponse(blog=json.dumps(blog))
        else:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details('Item not found')
            return domain_service_pb2.GetResponse()

    def ListBlogs(self, request, context):
        try:
            blogs_cursor = collection.find()
            blogs = []
            for blog in blogs_cursor:
                blog['id'] = str(blog['_id'])
                del blog['_id']
                blogs.append(domain_service_pb2.Blog(
                    id=blog['id'],
                    title=blog['title'],
                    author=blog['author'],
                    create_year=blog['create_year'],
                    text=blog['text']
                ))
            return domain_service_pb2.ListResponse(blogs=blogs)
        except Exception as e:
            logger.error(f"Error listing blogs: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details('Internal Server Error')
            return domain_service_pb2.ListResponse()
def initialize_data():
    dummy_data = {
        "title": "Первый блог",
        "author": "Автор 1",
        "create_year": "2025-01-08",
        "text": "Это текст первого блога."
    }
    try:
        collection.insert_one(dummy_data)
        logger.info("Тестовые данные добавлены в коллекцию blogs.")
    except Exception as e:
        logger.error(f"Ошибка при добавлении тестовых данных: {e}")

def handle_create(data):
    try:
        collection.insert_one(data)
        logger.info(f"Created blog: {data}")
    except Exception as e:
        logger.error(f"Error creating blog: {e}")

def handle_update(data):
    try:
        _id = ObjectId(data["id"])
        data.pop("id")
        collection.update_one({"_id": _id}, {"$set": data})
        logger.info(f"Updated blog with id {data['id']}: {data}")
    except Exception as e:
        logger.error(f"Error updating blog: {e}")

def handle_delete(data):
    try:
        collection.delete_one({"_id": ObjectId(data["id"])})
        logger.info(f"Deleted blog with id {data['id']}")
    except Exception as e:
        logger.error(f"Error deleting blog: {e}")


def callback(ch, method, properties, body):
    message = json.loads(body)
    operation = message.get('operation')
    data = message.get('data')
    logger.info(f"Received operation: {operation} with data: {data}")

    if operation == 'create':
        handle_create(data)
    elif operation == 'update':
        handle_update(data)
    elif operation == 'delete':
        handle_delete(data)
    else:
        logger.warning(f"Unknown operation: {operation}")

    ch.basic_ack(delivery_tag=method.delivery_tag)

def consume_messages():
    rabbit_channel.basic_qos(prefetch_count=1)
    rabbit_channel.basic_consume(queue='crud_operations', on_message_callback=callback)
    logger.info("Started consuming RabbitMQ messages")
    rabbit_channel.start_consuming()

def serve_grpc():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    domain_service_pb2_grpc.add_DomainServiceServicer_to_server(DomainServiceServicer(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    logger.info("gRPC server started on port 50051")
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        server.stop(0)
        logger.info("gRPC server stopped")

if __name__ == '__main__':
    import threading

    initialize_data()


    rabbit_thread = threading.Thread(target=consume_messages, daemon=True)
    rabbit_thread.start()

    serve_grpc()
