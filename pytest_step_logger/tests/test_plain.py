import time


def create_booking():
    time.sleep(2)


def call_api():
    time.sleep(3)


def validate_response():
    time.sleep(2)
    assert True


def test_example_plain():
    create_booking()
    call_api()
    validate_response()
