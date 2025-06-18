from pydantic import BaseSettings

class Settings(BaseSettings):
    rabbitmq_user: str
    rabbitmq_pass: str
    postgres_db: str
    postgres_user: str
    postgres_password: str
    celery_broker_url: str
    celery_backend_url: str
    database_url: str

    class Config:
        env_file = ".env"

settings = Settings()
