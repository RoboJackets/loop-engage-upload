"""
Upload data from Engage to Loop
"""
from argparse import ArgumentParser
from html import unescape
from json import dumps
from re import findall
from typing import Dict
from urllib.parse import parse_qs, urljoin, urlparse

from requests import get, post, put

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait

from webdriver_manager.chrome import ChromeDriverManager

from werkzeug.http import parse_options_header


def log_in_to_engage(driver: WebDriver, username: str, password: str) -> None:
    """
    Log in to Engage via CAS
    """
    print("Starting Engage authentication")
    driver.get("https://gatech.campuslabs.com/engage/account/login?returnUrl=/engage/")

    # wait for CAS login page to load
    WebDriverWait(driver, timeout=10).until(lambda d: d.find_element(By.ID, "username"))

    # enter username, password, and submit the form
    username_field = driver.find_element(By.ID, "username")
    password_field = driver.find_element(By.ID, "password")
    submit_button = driver.find_element(By.NAME, "submitbutton")

    print("Entering username")
    username_field.send_keys(username)
    print("Entering password")
    password_field.send_keys(password)
    print("Submitting login form")
    submit_button.click()

    # wait for Duo authentication to finish, redirect to Engage, and wait for Engage to fully load
    print("Waiting for authentication to complete")
    WebDriverWait(driver, timeout=20).until(lambda d: d.title == "Explore - Georgia Institute of Technology")


def sync_purchase_request(cookies: Dict[str, str], engage_id: str, server: str, token: str) -> None:
    """
    Sync a single purchase request from Engage to Loop
    """
    print(f"Retrieving purchase request {engage_id} from Engage")
    engage_response = get(
        url=f"https://gatech.campuslabs.com/engage/api/finance/robojackets/requests/purchase/{engage_id}/",
        cookies=cookies,
        timeout=(5, 5),
    )

    if engage_response.status_code != 200:
        print(engage_response.status_code)
        print(engage_response.text)
        raise ValueError("Unexpected response code from engage")

    print(f"Uploading purchase request {engage_id} to Loop")

    loop_response = put(
        url=f"{server}/api/v1/engage/purchase-requests/{engage_id}",
        json=engage_response.json(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=(5, 5),
    )

    if loop_response.status_code != 200:
        print(dumps(engage_response.json()))
        print(loop_response.status_code)
        print(loop_response.text)
        raise ValueError("Unexpected response code from Loop")

    print(loop_response.text)

    if loop_response.json()["deleted_at"] is None:
        engage_additional_questions_response = get(
            url=f"https://gatech.campuslabs.com/engage/actionCenter/organization/robojackets/finance/financeRequestViewAdditionalQuestions/{engage_id}",  # noqa: E501
            cookies=cookies,
            timeout=(5, 5),
        )

        if engage_additional_questions_response.status_code != 200:
            print(engage_id)
            print(engage_additional_questions_response.status_code)
            print(engage_additional_questions_response.text)
            raise ValueError("Unexpected response code from engage")

        matches = findall(
            r"/engage/actionCenter/organization/robojackets/Finance/FileUploadQuestion/getdocument\?DocumentId=[0-9]+&amp;RespondentId=[0-9]+",  # noqa: E501
            engage_additional_questions_response.text,
        )

        for match in matches:
            sync_attachment(cookies, engage_id, match, server, token)


def sync_attachment(cookies: Dict[str, str], engage_id: str, attachment_url: str, server: str, token: str) -> None:
    """
    Sync a single attachment from Engage to Loop
    """
    url_parts = urlparse(attachment_url, allow_fragments=False)

    query_string = parse_qs(url_parts.query)

    engage_document_id = query_string["DocumentId"][0]

    print(f"Checking if Loop already has attachment {engage_document_id}")

    loop_has_attachment_response = get(
        url=f"{server}/api/v1/engage/purchase-requests/{engage_id}/attachments/{engage_document_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=(5, 5),
    )

    if loop_has_attachment_response.status_code == 200:
        return

    if loop_has_attachment_response.status_code != 404:
        print(loop_has_attachment_response.status_code)
        print(loop_has_attachment_response.text)
        raise ValueError("Unexpected response code from Loop")

    print(f"Downloading attachment {engage_document_id} from Engage")

    engage_attachment_response = get(
        url=urljoin("https://gatech.campuslabs.com/", unescape(attachment_url)),
        cookies=cookies,
        timeout=(5, 5),
    )

    if engage_attachment_response.status_code != 200:
        print(engage_attachment_response.status_code)
        print(engage_attachment_response.text)
        raise ValueError("Unexpected response code from engage")

    (value, options) = parse_options_header(engage_attachment_response.headers.get("Content-Disposition"))

    print(f"Uploading attachment {engage_document_id} to Loop")

    loop_attachment_response = post(
        url=f"{server}/api/v1/engage/purchase-requests/{engage_id}/attachments",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        files={"attachment": (options["filename"], engage_attachment_response.content)},
        data={"documentId": engage_document_id},
        timeout=(5, 20),
    )

    if loop_attachment_response.status_code != 200:
        print(loop_attachment_response.status_code)
        print(loop_attachment_response.text)
        raise ValueError("Unexpected response code from Loop")


def main() -> None:
    """
    Entrypoint for script
    """
    parser = ArgumentParser(
        description="Upload data from Engage to Loop",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--server",
        help="the base URL of the Loop server",
        required=True,
    )
    parser.add_argument(
        "--token",
        help="the token to authenticate to Loop",
        required=True,
    )
    parser.add_argument(
        "--georgia-tech-username",
        help="the Georgia Tech username to authenticate to Engage",
        required=True,
    )
    parser.add_argument(
        "--georgia-tech-password",
        help="the Georgia Tech password to authenticate to Engage",
        required=True,
    )
    args = parser.parse_args()
    driver = webdriver.Chrome(service=Service(executable_path=ChromeDriverManager().install()))
    driver.maximize_window()

    log_in_to_engage(driver, args.georgia_tech_username, args.georgia_tech_password)

    cookies = {}

    for cookie in driver.get_cookies():
        cookies[cookie["name"]] = cookie["value"]

    print("Retrieving list of requests from Engage")
    engage_response = get(
        url="https://gatech.campuslabs.com/engage/api/finance/robojackets/requests/purchase/list-items",
        params={"take": 100},
        cookies=cookies,
        timeout=(5, 5),
    )
    print(engage_response.text)

    if engage_response.status_code != 200:
        print(engage_response.status_code)
        print(engage_response.text)
        raise ValueError("Unexpected response code from Engage")

    print("Uploading results to Loop")

    loop_response = post(
        url=f"{args.server}/api/v1/engage/purchase-requests",
        json=engage_response.json(),
        headers={
            "Authorization": f"Bearer {args.token}",
            "Accept": "application/json",
        },
        timeout=(5, 60),
    )

    if loop_response.status_code != 200:
        print(engage_response.text)
        print(loop_response.status_code)
        print(loop_response.text)
        raise ValueError("Unexpected response code from Loop")

    print(loop_response.status_code)
    print(loop_response.text)

    for purchase_request in loop_response.json()["requests"]:
        sync_purchase_request(cookies, purchase_request, args.server, args.token)

    loop_response = get(
        url=f"{args.server}/api/v1/engage/sync",
        headers={
            "Authorization": f"Bearer {args.token}",
            "Accept": "application/json",
        },
        timeout=(5, 5),
    )

    if loop_response.status_code != 200:
        print(loop_response.status_code)
        print(loop_response.text)
        raise ValueError("Unexpected response code from Loop")

    print(loop_response.status_code)
    print(loop_response.text)

    for purchase_request in loop_response.json()["requests"]:
        sync_purchase_request(cookies, purchase_request, args.server, args.token)

    loop_response = post(
        url=f"{args.server}/api/v1/engage/sync",
        headers={
            "Authorization": f"Bearer {args.token}",
            "Accept": "application/json",
        },
        timeout=(5, 5),
    )

    if loop_response.status_code != 200:
        print(loop_response.status_code)
        print(loop_response.text)
        raise ValueError("Unexpected response code from Loop")


if __name__ == "__main__":
    main()
