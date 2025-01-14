from typing import List
from uuid import uuid4
from time import time, sleep
import pytest
from tests import conftest
from aiohttp import ClientResponse
from api_test_utils import env
from api_test_utils import poll_until
from api_test_utils.api_session_client import APISessionClient
from api_test_utils.api_test_session_config import APITestSessionConfig


def dict_path(raw, path: List[str]):
    if not raw:
        return raw

    if not path:
        return raw

    res = raw.get(path[0])
    if not res or len(path) == 1 or type(res) != dict:
        return res

    return dict_path(res, path[1:])


@pytest.mark.e2e
@pytest.mark.smoketest
def test_output_test_config(api_test_config: APITestSessionConfig):
    print(api_test_config)


@pytest.mark.e2e
@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_wait_for_ping(
    api_client: APISessionClient, api_test_config: APITestSessionConfig
):
    async def apigee_deployed(resp: ClientResponse):
        if resp.status != 200:
            return False
        body = await resp.json()

        return body.get("commitId") == api_test_config.commit_id

    await poll_until(
        make_request=lambda: api_client.get("_ping"), until=apigee_deployed, timeout=30
    )


@pytest.mark.e2e
@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_check_status_is_secured(api_client: APISessionClient):

    async with api_client.get("_status", allow_retries=True) as resp:
        assert resp.status == 401


@pytest.mark.e2e
@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_wait_for_status(
    api_client: APISessionClient, api_test_config: APITestSessionConfig
):
    async def is_deployed(resp: ClientResponse):
        if resp.status != 200:
            return False
        body = await resp.json()

        if body.get("commitId") != api_test_config.commit_id:
            return False

        backend = dict_path(body, ["checks", "healthcheck", "outcome", "version"])
        if not backend:
            return True

        return backend.get("commitId") == api_test_config.commit_id

    deploy_timeout = 120 if api_test_config.api_environment.endswith("sandbox") else 30

    await poll_until(
        make_request=lambda: api_client.get(
            "_status", headers={"apikey": env.status_endpoint_api_key()}
        ),
        until=is_deployed,
        timeout=deploy_timeout,
    )


def _base_valid_uri(nhs_number) -> str:
    return f"FHIR/R4/Immunization?patient.identifier=https://fhir.nhs.uk/Id/nhs-number|{nhs_number}"


def _valid_uri(nhs_number, procedure_code) -> str:
    return _base_valid_uri(nhs_number) + f"&procedure-code:below={procedure_code}"


@pytest.fixture(scope='function')
def authorised_headers(valid_access_token):
    return {"Authorization": f"Bearer {valid_access_token}"}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_check_immunization_is_secured(api_client: APISessionClient):

    async with api_client.get(_base_valid_uri("9912003888"), allow_retries=True) as resp:
        assert resp.status == 401


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_immunization_happy_path(test_app, api_client: APISessionClient, authorised_headers):

    correlation_id = str(uuid4())
    authorised_headers["X-Correlation-ID"] = correlation_id
    authorised_headers["NHSD-User-Identity"] = conftest.nhs_login_id_token(test_app)

    async with api_client.get(
        _valid_uri("9912003888", "90640007"),
        headers=authorised_headers,
        allow_retries=True
    ) as resp:
        assert resp.status == 200
        body = await resp.json()
        assert "x-correlation-id" in resp.headers, resp.headers
        assert resp.headers["x-correlation-id"] == correlation_id
        assert body["resourceType"] == "Bundle", body
        # no data for this nhs number ...
        assert len(body["entry"]) == 0, body


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_data",
    [
        # condition 1: invalid iss claim
        {
            "expected_status_code": 401,
            "expected_response": {
                "severity": "error",
                "error_code": "processing",
                "error_diagnostics": "Missing or invalid 'iss' claim in ID Token",
            },
            "claims": {
                "iss": "invalid"
            }
        },
        # condition 2: invalid typ header
        {
            "expected_status_code": 401,
            "expected_response": {
                "severity": "error",
                "error_code": "processing",
                "error_diagnostics": "Missing or invalid 'typ' header in ID Token - must be 'JWT'",
            },
            "headers": {
                "typ": "invalid"
            }
        },
        # condition 3: invalid identity_proofing_level claim
        {
            "expected_status_code": 401,
            "expected_response": {
                "severity": "error",
                "error_code": "processing",
                "error_diagnostics": "Missing or invalid 'identity_proofing_level' claim in ID Token",
            },
            "claims": {
                "identity_proofing_level": "invalid"
            }
        },
        # condition 4: jwt expired
        {
            "expected_status_code": 401,
            "expected_response": {
                "severity": "error",
                "error_code": "processing",
                "error_diagnostics": "Invalid exp claim in JWT - JWT has expired",
            },
            "claims": {
                "exp": int(time()) - 10,  # Set JWT as already expired
                "iat": int(time()) - 10
            }
        },
        # condition 5: invalid id token
        {
            "expected_status_code": 401,
            "expected_response": {
                "severity": "error",
                "error_code": "processing",
                "error_diagnostics": "Malformed JWT in 'NHSD-User-Identity' header",
            },
            "id_token": "invalid"
        },
        # condition 6: no matching public key
        {
            "expected_status_code": 401,
            "expected_response": {
                "severity": "error",
                "error_code": "processing",
                "error_diagnostics": "Could not find a matching Public Key",
            },
            "headers": {
                "kid": "invalid"
            }
        },
        # condition 7: catch all error message
        {
            "expected_status_code": 401,
            "expected_response": {
                "severity": "error",
                "error_code": "processing",
                "error_diagnostics": "Failed to verify JWT",
            },
            "headers": {
                "alg": "invalid"
            }
        },
    ],
)
async def test_immunisation_id_token_error_scenarios(test_app,
                                                     api_client: APISessionClient,
                                                     authorised_headers, request_data: dict):
    sleep(1)  # Add delay to tests to avoid 429 on service callout
    id_token = conftest.nhs_login_id_token(
        test_app=test_app,
        id_token_claims=request_data.get("claims"),
        id_token_headers=request_data.get("headers")
    )

    if request_data.get("id_token") is not None:
        authorised_headers["NHSD-User-Identity"] = request_data.get("id_token")
    else:
        authorised_headers["NHSD-User-Identity"] = id_token

    async with api_client.get(
        _valid_uri("9912003888", "90640007"),
        headers=authorised_headers,
        allow_retries=True
    ) as resp:
        assert resp.status == request_data["expected_status_code"]
        body = await resp.json()
        assert body["resourceType"] == "OperationOutcome"
        assert body["issue"][0]["severity"] == request_data["expected_response"]["severity"]
        assert body["issue"][0]["diagnostics"] == request_data["expected_response"]["error_diagnostics"]
        assert body["issue"][0]["code"] == request_data["expected_response"]["error_code"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_immunization_no_jwt_header_provided(api_client: APISessionClient, authorised_headers):

    sleep(1)  # Add delay to tests to avoid 429 on service callout

    async with api_client.get(
        _valid_uri("9912003888", "90640007"),
        headers=authorised_headers,
        allow_retries=True
    ) as resp:
        assert resp.status == 401
        body = await resp.json()
        assert body["resourceType"] == "OperationOutcome"
        assert body["issue"][0]["severity"] == "error"
        assert body["issue"][0]["diagnostics"] == "Missing value in header 'NHSD-User-Identity'"
        assert body["issue"][0]["code"] == "processing"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_bad_nhs_number(test_app, api_client: APISessionClient, authorised_headers):

    sleep(1)  # Add delay to tests to avoid 429 on service callout

    authorised_headers["NHSD-User-Identity"] = conftest.nhs_login_id_token(test_app)

    async with api_client.get(
        _valid_uri("90000000009", "90640007"),
        headers=authorised_headers,
        allow_retries=True
    ) as resp:
        assert resp.status == 400
        body = await resp.json()
        assert body["resourceType"] == "OperationOutcome", body
        issue = next((i for i in body.get('issue', []) if i.get('severity') == 'error'), None)
        assert issue.get("diagnostics") == "Missing required request parameters: [patient.identifier]", body


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_correlation_id_mirrored_in_resp_when_error(
    api_client: APISessionClient
):
    access_token = "invalid_token"

    correlation_id = str(uuid4())

    async with api_client.get(
        _valid_uri("9912003888", "90640007"),
        headers={"Authorization": f"Bearer {access_token}", "X-Correlation-ID": correlation_id},
        allow_retries=True
    ) as resp:
        assert resp.status == 401
        assert "x-correlation-id" in resp.headers, resp.headers
        assert resp.headers["x-correlation-id"] == correlation_id, resp.headers


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_user_restricted_access_not_permitted(api_client: APISessionClient, test_product_and_app):
    sleep(1)  # Add delay to tests to avoid 429 on service callout

    test_product, test_app = test_product_and_app

    await test_product.update_scopes(["urn:nhsd:apim:user-nhs-id:aal3:immunisation-history"])
    await test_app.add_api_product([test_product.name])

    token_response = await conftest.get_token(test_app)

    authorised_headers = {
        "Authorization": f"Bearer {token_response['access_token']}",
        "NHSD-User-Identity": conftest.nhs_login_id_token(test_app)
    }

    async with api_client.get(
        _valid_uri("9912003888", "90640007"),
        headers=authorised_headers,
        allow_retries=True
    ) as resp:
        assert resp.status == 401
        body = await resp.json()
        assert body["resourceType"] == "OperationOutcome"
        assert body["issue"][0]["severity"] == "error"
        assert body["issue"][0]["diagnostics"] == "Provided access token is invalid"
        assert body["issue"][0]["code"] == "forbidden"
