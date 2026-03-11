import allure
import time
import pytest_check as check

@allure.step("Create booking")
def create_booking():
    time.sleep(3)

@allure.step("Call API")
def call_api():
    time.sleep(3)
    check.equal(2, 2)
    pass

@allure.step("Validate response")
def validate_response():
    time.sleep(2)
    check.equal(1, 1)

def test_example(user_session):
    create_booking()
    call_api()
    validate_response()