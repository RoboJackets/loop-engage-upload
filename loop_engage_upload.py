"""
Upload data from Engage to Loop
"""

from argparse import ArgumentParser
from html import unescape
from json import dumps
from re import findall
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup

from requests import Response, Session, get, post, put

from werkzeug.http import parse_options_header

DEFAULT_CAS_HOST = "sso.gatech.edu"
ENGAGE_CAS_SERVICE = "https://federation.campuslabs.com/auth/signin/"
ENGAGE_LOGIN_URL = "https://gatech.campuslabs.com/engage/account/login?returnUrl=/engage/"
GET_TGT_URL = "https://{hostname}/cas/v1/tickets"
HTML_PARSER = "html.parser"


def get_ticket_granting_ticket_url(
    hostname: str, session: Session, username: str, password: str
) -> Tuple[int, Optional[str]]:
    """
    Exchanges a username and password for a TGT

    Duo push is handled by CAS during this request.
    """
    response = session.post(
        GET_TGT_URL.format(hostname=hostname),
        data={"username": username, "password": password},
    )

    if response.status_code in (423, 401):
        return response.status_code, None

    tgt_url = BeautifulSoup(response.text, HTML_PARSER).form["action"]  # type: ignore

    assert isinstance(tgt_url, str)

    return 200, tgt_url


def start_engage_sso(session: Session) -> None:
    """
    Follow Engage login redirects through CampusLabs IdentityServer/federation

    Stops once CAS is about to be invoked, after federation has set FpContext and
    Engage/IdentityServer have correlation cookies needed to finish the login.
    """
    url = ENGAGE_LOGIN_URL
    for _ in range(15):
        response = session.get(url, allow_redirects=False, timeout=(5, 30))
        location = response.headers.get("Location")

        if location is None or response.status_code not in (301, 302, 303, 307, 308):
            print(response.status_code)
            print(response.text[:500])
            raise ValueError("Unexpected response while starting Engage SSO")

        if location.startswith("/"):
            next_url = urljoin(url, location)
        else:
            next_url = location

        if "cas/login" in next_url or "login.gatech.edu" in next_url or "sso.gatech.edu" in next_url:
            return

        url = next_url

    raise ValueError("Exceeded redirect limit while starting Engage SSO")


def submit_auto_post_forms(session: Session, response: Response) -> None:
    """
    Submit browser-style auto-POST forms (WS-Fed / OIDC callbacks) until done
    """
    for _ in range(10):
        soup = BeautifulSoup(response.text, HTML_PARSER)
        form = soup.find("form")
        if form is None:
            return

        action = form.get("action")
        if not isinstance(action, str) or not action:
            return

        action_url = urljoin(response.url, action)
        method_attr = form.get("method")
        method = method_attr.lower() if isinstance(method_attr, str) else "get"
        payload: Dict[str, str] = {}
        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            if isinstance(name, str) and name:
                value = input_tag.get("value")
                payload[name] = value if isinstance(value, str) else ""

        if method == "post":
            response = session.post(action_url, data=payload, timeout=(5, 60))
        else:
            response = session.get(action_url, params=payload, timeout=(5, 60))

        if response.status_code != 200:
            print(response.status_code)
            print(response.text[:500])
            raise ValueError("Unexpected response while completing CampusLabs SSO callbacks")

    raise ValueError("Exceeded form-submit limit while completing CampusLabs SSO")


def log_in_to_engage(session: Session, username: str, password: str) -> None:
    """
    Log in to Engage via CAS REST ticket API
    """
    print("Starting Engage authentication")
    print("Starting CampusLabs SSO redirect chain")
    start_engage_sso(session)

    print("Requesting CAS ticket-granting ticket (approve Duo push if prompted)")
    status_code, tgt_url = get_ticket_granting_ticket_url(DEFAULT_CAS_HOST, session, username, password)

    if status_code == 401:
        raise ValueError("Invalid Georgia Tech username or password")

    if status_code == 423:
        raise ValueError("CAS returned a 423 error (account locked or similar)")

    if tgt_url is None:
        raise ValueError("Failed to retrieve a CAS ticket-granting ticket")

    print("Exchanging TGT for Engage service ticket")
    service_ticket_response = session.post(tgt_url, data={"service": ENGAGE_CAS_SERVICE})

    if service_ticket_response.status_code != 200:
        print(service_ticket_response.status_code)
        print(service_ticket_response.text)
        raise ValueError("Unexpected response code from CAS while retrieving service ticket")

    service_ticket = service_ticket_response.text
    print("Redeeming service ticket at CampusLabs federation")
    federation_response = session.get(
        ENGAGE_CAS_SERVICE + "?ticket=" + quote(service_ticket),
        timeout=(5, 60),
    )

    if federation_response.status_code != 200:
        print(federation_response.status_code)
        print(federation_response.text)
        raise ValueError("Unexpected response code from CampusLabs federation")

    print("Completing CampusLabs SSO callbacks")
    submit_auto_post_forms(session, federation_response)

    print("Following through to Engage")
    engage_response = session.get(
        "https://gatech.campuslabs.com/engage/",
        timeout=(5, 30),
    )
    if engage_response.status_code != 200:
        print(engage_response.status_code)
        print(engage_response.text[:500])
        raise ValueError("Unexpected response code from Engage after login")

    print("Engage authentication complete")


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

    value, options = parse_options_header(engage_attachment_response.headers.get("Content-Disposition"))

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
    session = Session()
    log_in_to_engage(session, args.georgia_tech_username, args.georgia_tech_password)
    cookies = session.cookies.get_dict()

    print("Retrieving list of requests from Engage")
    engage_response = get(
        url="https://gatech.campuslabs.com/engage/api/finance/robojackets/requests/purchase/list-items",
        params={"take": 200},
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
