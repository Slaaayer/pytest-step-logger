import time
import pytest


@pytest.fixture
def db_connection():
    time.sleep(0.3)
    yield {"host": "localhost"}
    time.sleep(0.2)


@pytest.fixture
def user_session(db_connection):
    time.sleep(0.15)
    yield {"user": "test_user", "db": db_connection}
    time.sleep(0.1)
