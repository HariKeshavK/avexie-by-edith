import asyncio
import base64
import fnmatch
import hashlib
import logging
import re
import sys
import urllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partialmethod
from types import SimpleNamespace
from typing import Literal, Optional

import aiohttp
import jwt
from authlib.integrations.starlette_client import OAuth
from authlib.oauth2.rfc6749.errors import OAuth2Error
from authlib.oidc.core import UserInfo
from cryptography.fernet import Fernet, InvalidToken
from fastapi import (
    HTTPException,
    status,
)
from joserfc.errors import BadSignatureError
from joserfc.jws import JWSRegistry
from mcp.shared.auth import (
    OAuthClientMetadata as MCPOAuthClientMetadata,
)
from mcp.shared.auth import (
    OAuthMetadata,
)
from avexie.config import (
    OAUTH_CLIENT_TIMEOUT,
)
from avexie.env import (
    AIOHTTP_CLIENT_ALLOW_REDIRECTS,
    AIOHTTP_CLIENT_SESSION_SSL,
    OAUTH_CLIENT_INFO_ENCRYPTION_KEY,
)
from avexie.models.config import Config
from avexie.models.oauth_sessions import OAuthSessions
from avexie.retrieval.web.utils import get_ssrf_safe_session, validate_url
from avexie.utils.auth import (
    get_optional_verified_user_from_request,
    get_verified_user_by_id,
)
from starlette.responses import RedirectResponse

# Some IdPs put private params in ID token JOSE headers (CAS: client_id, CyberArk: app_id).
# Authlib exposes no way to pass a registry, so relax it globally; crit, alg and signature checks still apply.
JWSRegistry.__init__ = partialmethod(JWSRegistry.__init__, strict_check_header=False)


class OAuthClientMetadata(MCPOAuthClientMetadata):
    token_endpoint_auth_method: Literal['none', 'client_secret_basic', 'client_secret_post'] = 'client_secret_post'
    pass


OAuthResourceParameterMode = Literal['auto', 'include', 'omit']


class OAuthClientInformationFull(OAuthClientMetadata):
    issuer: Optional[str] = None  # URL of the OAuth server that issued this client
    resource: Optional[str] = None  # RFC 8707 resource indicator for JWT audience
    oauth_resource_parameter: OAuthResourceParameterMode = 'auto'

    client_id: str
    client_secret: str | None = None
    client_id_issued_at: int | None = None
    client_secret_expires_at: int | None = None

    server_metadata: Optional[OAuthMetadata] = None  # Fetched from the OAuth server


from avexie.env import GLOBAL_LOG_LEVEL
from avexie.utils.json_codec import JSONCodec

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

OAUTH_RESOURCE_PARAMETER_MODES = {'auto', 'include', 'omit'}


# Conservative default when the provider omits both expires_in and expires_at.
# Matches the value recommended by Authlib's compliance_fix documentation.
DEFAULT_TOKEN_EXPIRY_SECONDS = 3600
NON_EXPIRING_TOKEN_EXPIRES_AT = 253402300799  # 9999-12-31 23:59:59 UTC


def _normalize_token_expiry(token: dict) -> dict:
    """Ensure a token dict always has a numeric ``expires_at``.

    Resolution order:
    1. If *expires_at* is already present and non-None, trust it.
    2. Else if *expires_in* is present and non-None, compute *expires_at*.
    3. Else if a *refresh_token* is present, fall back to
       ``DEFAULT_TOKEN_EXPIRY_SECONDS`` and log a warning so operators can
       identify providers that omit expiration.
    4. Otherwise treat the token as non-expiring; there is no refresh path to
       recover from a fabricated short expiry.

    Also stamps *issued_at* for auditing.
    """
    token['issued_at'] = datetime.now().timestamp()

    if token.get('expires_at') is not None:
        expires_at = int(token['expires_at'])
    elif token.get('expires_in') is not None:
        expires_at = int(datetime.now().timestamp() + token['expires_in'])
    elif token.get('refresh_token'):
        log.warning(
            "OAuth token response missing both 'expires_in' and 'expires_at'; "
            f'defaulting to {DEFAULT_TOKEN_EXPIRY_SECONDS}s from now'
        )
        expires_at = int(datetime.now().timestamp() + DEFAULT_TOKEN_EXPIRY_SECONDS)
    else:
        log.info(
            "OAuth token response missing 'expires_in', 'expires_at' and 'refresh_token'; treating token as non-expiring"
        )
        expires_at = NON_EXPIRING_TOKEN_EXPIRES_AT

    id_token = token.get('id_token')
    if id_token:
        # Cap at the id_token expiry so pipes and tools never receive an expired JWT
        try:
            exp = jwt.decode(id_token, options={'verify_signature': False}).get('exp')
            if exp is not None:
                expires_at = min(expires_at, int(exp))
        except Exception as e:
            log.debug('Could not read exp from id_token: %s', e)

    token['expires_at'] = expires_at
    return token


FERNET = None

if len(OAUTH_CLIENT_INFO_ENCRYPTION_KEY) != 44:
    key_bytes = hashlib.sha256(OAUTH_CLIENT_INFO_ENCRYPTION_KEY.encode()).digest()
    OAUTH_CLIENT_INFO_ENCRYPTION_KEY = base64.urlsafe_b64encode(key_bytes)
else:
    OAUTH_CLIENT_INFO_ENCRYPTION_KEY = OAUTH_CLIENT_INFO_ENCRYPTION_KEY.encode()

try:
    FERNET = Fernet(OAUTH_CLIENT_INFO_ENCRYPTION_KEY)
except Exception as e:
    log.error(f'Error initializing Fernet with provided key: {e}')
    raise


def encrypt_data(data) -> str:
    """Encrypt data for storage"""
    try:
        data_json = JSONCodec.dumps(data)
        encrypted = FERNET.encrypt(data_json.encode()).decode()
        return encrypted
    except Exception as e:
        log.error(f'Error encrypting data: {e}')
        raise


def decrypt_data(data: str):
    """Decrypt data from storage"""
    decrypted = FERNET.decrypt(data.encode()).decode()
    return JSONCodec.loads(decrypted)




def get_parsed_and_base_url(server_url) -> tuple[urllib.parse.ParseResult, str]:
    parsed = urllib.parse.urlparse(server_url)
    base_url = f'{parsed.scheme}://{parsed.netloc}'
    return parsed, base_url


@dataclass
class ProtectedResourceMetadata:
    """RFC 9728 Protected Resource Metadata fields relevant to OAuth flows."""

    resource: str | None = None
    authorization_servers: list[str] = field(default_factory=list)
    scopes_supported: list[str] = field(default_factory=list)

    def get_discovery_urls(self, server_url: str) -> list[str]:
        """Build all candidate OAuth discovery URLs from this metadata and the server URL."""
        urls = []
        for auth_server in self.authorization_servers:
            urls.extend(_build_well_known_urls(auth_server.rstrip('/')))
        urls.extend(_build_well_known_urls(server_url))
        return urls


async def get_protected_resource_metadata(server_url: str) -> ProtectedResourceMetadata:
    """
    Fetch RFC 9728 Protected Resource Metadata from an MCP server.

    https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization

    Returns:
        ProtectedResourceMetadata with the resource indicator (RFC 8707)
        and authorization server URLs discovered from the metadata document.
    """
    authorization_servers = []
    resource = None
    scopes = []
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                server_url,
                json={'jsonrpc': '2.0', 'method': 'initialize', 'params': {}, 'id': 1},
                headers={'Content-Type': 'application/json'},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                # Discover Protected Resource Metadata regardless of HTTP status.
                # A 401 carries a WWW-Authenticate header pointing at the PRM, but
                # some MCP servers (e.g. Google's gmail/drive/calendar remote MCPs)
                # answer 200 to an anonymous `initialize`, so we must still fall
                # back to the RFC 9728 well-known URIs when there is no 401/header.
                resource_metadata_urls = []
                match = re.search(
                    r'resource_metadata=(?:"([^"]+)"|([^\s,]+))',
                    response.headers.get('WWW-Authenticate', ''),
                )
                if match:
                    resource_metadata_urls = [match.group(1) or match.group(2)]
                    log.debug('Found resource_metadata URL: %s', resource_metadata_urls[0])
                else:
                    # Fall back to well-known resource metadata URIs (RFC 9728 §4.2)
                    parsed, base_url = get_parsed_and_base_url(server_url)
                    if parsed.path and parsed.path != '/':
                        path = parsed.path.rstrip('/')
                        resource_metadata_urls.append(
                            urllib.parse.urljoin(base_url, f'/.well-known/oauth-protected-resource{path}')
                        )
                    resource_metadata_urls.append(
                        urllib.parse.urljoin(base_url, '/.well-known/oauth-protected-resource')
                    )
                    log.debug('No resource_metadata in header, trying well-known URIs: %s', resource_metadata_urls)

                # Fetch Protected Resource metadata from candidate URLs
                for resource_metadata_url in resource_metadata_urls:
                    try:
                        async with session.get(
                            resource_metadata_url, ssl=AIOHTTP_CLIENT_SESSION_SSL
                        ) as resource_response:
                            if resource_response.status == 200:
                                resource_metadata = await resource_response.json()

                                resource = resource_metadata.get('resource') or None
                                if resource:
                                    log.debug('Discovered resource indicator: %s', resource)

                                servers = resource_metadata.get('authorization_servers', [])
                                scopes = resource_metadata.get('scopes_supported', [])
                                if scopes:
                                    log.debug('Discovered resource scopes: %s', scopes)

                                if servers:
                                    authorization_servers = servers
                                    log.debug('Discovered authorization servers: %s', servers)
                                    break
                    except Exception as e:
                        log.debug('Failed to fetch resource metadata from %s: %s', resource_metadata_url, e)
                        continue
    except Exception as e:
        log.debug('MCP Protected Resource discovery failed: %s', e)

    return ProtectedResourceMetadata(
        resource=resource, authorization_servers=authorization_servers, scopes_supported=scopes
    )


def _build_well_known_urls(server_url: str) -> list[str]:
    """Build RFC 8414 / OIDC Discovery well-known URLs for a server URL."""
    parsed, base_url = get_parsed_and_base_url(server_url)
    urls = []

    if parsed.path and parsed.path != '/':
        path = parsed.path.rstrip('/')
        urls.extend(
            [
                urllib.parse.urljoin(base_url, f'/.well-known/oauth-authorization-server{path}'),
                urllib.parse.urljoin(base_url, f'/.well-known/openid-configuration{path}'),
                urllib.parse.urljoin(base_url, f'{path}/.well-known/openid-configuration'),
            ]
        )

    urls.extend(
        [
            urllib.parse.urljoin(base_url, '/.well-known/oauth-authorization-server'),
            urllib.parse.urljoin(base_url, '/.well-known/openid-configuration'),
        ]
    )

    return urls


async def get_discovery_urls(server_url) -> list[str]:
    """Convenience: get all OAuth discovery URLs for a server URL."""
    metadata = await get_protected_resource_metadata(server_url)
    return metadata.get_discovery_urls(server_url)


# TODO: Some OAuth providers require Initial Access Tokens (IATs) for dynamic client registration.
# This is not currently supported.
async def get_oauth_client_info_with_dynamic_client_registration(
    request,
    client_id: str,
    oauth_server_url: str,
    oauth_server_key: Optional[str] = None,
    oauth_scope: str | None = None,
) -> OAuthClientInformationFull:
    try:
        oauth_server_metadata = None
        oauth_server_metadata_url = None

        webui_url = await Config.get('webui.url')
        redirect_base_url = (str(webui_url or request.base_url)).rstrip('/')

        oauth_client_metadata = OAuthClientMetadata(
            # LICENSE covers this AVEXIE OAuth client identifier.
            # Do not alter, remove, obscure, or replace it except as LICENSE permits:
            # https://docs.openwebui.com/license.
            client_name='AVEXIE',
            redirect_uris=[f'{redirect_base_url}/oauth/clients/{client_id}/callback'],
            grant_types=['authorization_code', 'refresh_token'],
            response_types=['code'],
        )

        # Attempt to fetch OAuth server metadata to get registration endpoint & scopes
        resource_metadata = await get_protected_resource_metadata(oauth_server_url)
        resource = resource_metadata.resource

        # Prefer the resource-specific scopes from the Protected Resource Metadata
        # (RFC 9728) over the AS's full scopes_supported catalog, for least
        # privilege. Mirrors the static-credentials flow (#24690).
        scope_override = ' '.join(oauth_scope.replace(',', ' ').split()) if oauth_scope else None
        if scope_override:
            oauth_client_metadata.scope = scope_override
        elif resource_metadata.scopes_supported:
            oauth_client_metadata.scope = ' '.join(resource_metadata.scopes_supported)

        discovery_urls = resource_metadata.get_discovery_urls(oauth_server_url)
        for url in discovery_urls:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, ssl=AIOHTTP_CLIENT_SESSION_SSL) as oauth_server_metadata_response:
                    if oauth_server_metadata_response.status == 200:
                        try:
                            oauth_server_metadata = OAuthMetadata.model_validate(
                                await oauth_server_metadata_response.json()
                            )
                            oauth_server_metadata_url = url
                            if (
                                oauth_client_metadata.scope is None
                                and oauth_server_metadata.scopes_supported is not None
                            ):
                                oauth_client_metadata.scope = ' '.join(oauth_server_metadata.scopes_supported)

                            if (
                                oauth_server_metadata.token_endpoint_auth_methods_supported
                                and oauth_client_metadata.token_endpoint_auth_method
                                not in oauth_server_metadata.token_endpoint_auth_methods_supported
                            ):
                                # Pick the first supported method from the server
                                oauth_client_metadata.token_endpoint_auth_method = (
                                    oauth_server_metadata.token_endpoint_auth_methods_supported[0]
                                )

                            break
                        except Exception as e:
                            log.error(f'Error parsing OAuth metadata from {url}: {e}')
                            continue

        # Fail fast if authorization server metadata discovery did not resolve an
        # authorization endpoint. Otherwise registration can still "succeed" (via
        # the /register fallback below) while issuer/server_metadata stay unset,
        # which later crashes at authorize time with authlib's
        # RuntimeError: Missing "authorize_url" value. (#26647)
        if oauth_server_metadata is None or not oauth_server_metadata.authorization_endpoint:
            log.error(f'OAuth authorization server metadata discovery failed for {oauth_server_url}')
            raise Exception(
                'Could not discover the OAuth authorization server metadata '
                f'(authorization_endpoint) for {oauth_server_url}. The MCP server must '
                'expose RFC 8414 / RFC 9728 discovery documents so AVEXIE can '
                'resolve where to send users to authorize.'
            )

        registration_url = None
        if oauth_server_metadata and oauth_server_metadata.registration_endpoint:
            registration_url = str(oauth_server_metadata.registration_endpoint)
        else:
            _, base_url = get_parsed_and_base_url(oauth_server_url)
            registration_url = urllib.parse.urljoin(base_url, '/register')

        registration_data = oauth_client_metadata.model_dump(
            exclude_none=True,
            mode='json',
            by_alias=True,
        )

        # Perform dynamic client registration and return client info
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                registration_url, json=registration_data, ssl=AIOHTTP_CLIENT_SESSION_SSL
            ) as oauth_client_registration_response:
                try:
                    registration_response_json = await oauth_client_registration_response.json()

                    # The mcp package requires optional unset values to be None. If an empty string is passed, it gets validated and fails.
                    # This replaces all empty strings with None.
                    registration_response_json = {
                        k: (None if v == '' else v) for k, v in registration_response_json.items()
                    }
                    oauth_client_info = OAuthClientInformationFull.model_validate(
                        {
                            **registration_response_json,
                            'issuer': oauth_server_metadata_url,
                            'server_metadata': oauth_server_metadata,
                            'resource': resource,
                        }
                    )
                    log.info(
                        'Dynamic client registration successful at %s, client_id: %s',
                        registration_url,
                        oauth_client_info.client_id,
                    )
                    return oauth_client_info
                except Exception as e:
                    error_text = None
                    try:
                        error_text = await oauth_client_registration_response.text()
                        log.error(
                            f'Dynamic client registration failed at {registration_url}: {oauth_client_registration_response.status} - {error_text}'
                        )
                    except Exception as e:
                        pass

                    log.error(f'Error parsing client registration response: {e}')
                    raise Exception(
                        f'Dynamic client registration failed: {error_text}'
                        if error_text
                        else 'Error parsing client registration response'
                    )
        raise Exception('Dynamic client registration failed')
    except Exception as e:
        log.error(f'Exception during dynamic client registration: {e}')
        raise e


async def get_oauth_client_info_with_static_credentials(
    request,
    client_id: str,
    oauth_server_url: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    oauth_scope: str | None = None,
) -> OAuthClientInformationFull:
    """
    Build an OAuthClientInformationFull from user-provided static credentials.
    Performs server metadata discovery to resolve authorization/token endpoints,
    but skips dynamic client registration entirely.
    """
    try:
        oauth_server_metadata = None
        oauth_server_metadata_url = None

        webui_url = await Config.get('webui.url')
        redirect_base_url = (str(webui_url or request.base_url)).rstrip('/')
        redirect_uri = f'{redirect_base_url}/oauth/clients/{client_id}/callback'

        # Discover server metadata (authorization endpoint, token endpoint, scopes, etc.)
        resource_metadata = await get_protected_resource_metadata(oauth_server_url)
        resource = resource_metadata.resource
        discovery_urls = resource_metadata.get_discovery_urls(oauth_server_url)
        for url in discovery_urls:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, ssl=AIOHTTP_CLIENT_SESSION_SSL) as resp:
                    if resp.status == 200:
                        try:
                            oauth_server_metadata = OAuthMetadata.model_validate(await resp.json())
                            oauth_server_metadata_url = url
                            break
                        except Exception as e:
                            log.error(f'Error parsing OAuth metadata from {url}: {e}')
                            continue

        # Use scopes from the Protected Resource Metadata (RFC 9728) if available.
        # Unlike the Authorization Server's scopes_supported (which is a full catalog
        # of every scope the server can grant), the PRM scopes_supported represents
        # what this specific resource requires — making it safe to request them all.
        scope = (' '.join(oauth_scope.replace(',', ' ').split()) if oauth_scope else None) or (
            ' '.join(resource_metadata.scopes_supported) if resource_metadata.scopes_supported else None
        )

        # Determine token_endpoint_auth_method
        token_endpoint_auth_method = 'client_secret_post'
        if (
            oauth_server_metadata
            and oauth_server_metadata.token_endpoint_auth_methods_supported
            and token_endpoint_auth_method not in oauth_server_metadata.token_endpoint_auth_methods_supported
        ):
            token_endpoint_auth_method = oauth_server_metadata.token_endpoint_auth_methods_supported[0]

        oauth_client_info = OAuthClientInformationFull(
            client_id=oauth_client_id,
            client_secret=oauth_client_secret,
            redirect_uris=[redirect_uri],
            grant_types=['authorization_code', 'refresh_token'],
            response_types=['code'],
            scope=scope,
            token_endpoint_auth_method=token_endpoint_auth_method,
            issuer=oauth_server_metadata_url,
            server_metadata=oauth_server_metadata,
            resource=resource,
        )

        log.info(
            'Static OAuth client info built for %s using metadata from %s', oauth_client_id, oauth_server_metadata_url
        )
        return oauth_client_info
    except Exception as e:
        log.error(f'Exception building static OAuth client info: {e}')
        raise e


def resolve_oauth_client_info(connection: dict) -> dict:
    """
    Decrypt OAuth client info from a tool server connection config.

    For oauth_2.1_static, overlays admin-provided credentials from
    info.oauth_client_id and info.oauth_client_secret onto the blob.
    """
    info = connection.get('info') or {}
    data = decrypt_data(info.get('oauth_client_info', ''))

    if connection.get('auth_type') == 'oauth_2.1_static':
        if info.get('oauth_client_id') and info.get('oauth_client_secret'):
            data['client_id'] = info['oauth_client_id']
            data['client_secret'] = info['oauth_client_secret']

    return data


def normalize_oauth_resource_parameter(value: str | None) -> OAuthResourceParameterMode:
    if value in OAUTH_RESOURCE_PARAMETER_MODES:
        return value
    return 'auto'


def get_connection_oauth_resource_parameter(connection: dict) -> OAuthResourceParameterMode:
    info = connection.get('info') or {}
    config = connection.get('config') or {}
    return normalize_oauth_resource_parameter(
        info.get('oauth_resource_parameter') or config.get('oauth_resource_parameter')
    )


def apply_connection_oauth_options(connection: dict, oauth_client_info: dict) -> dict:
    info = connection.get('info') or {}
    config = connection.get('config') or {}
    oauth_scope = info.get('oauth_scope') or config.get('oauth_scope')
    oauth_scope = ' '.join(oauth_scope.replace(',', ' ').split()) if oauth_scope else None

    options = {
        **oauth_client_info,
        'oauth_resource_parameter': get_connection_oauth_resource_parameter(connection),
    }
    if oauth_scope:
        options['scope'] = oauth_scope
    return options


def scope_has_resource_indicator(scope: str | None) -> bool:
    if not scope:
        return False
    return any(scope_value.startswith(('https://', 'http://', 'api://')) for scope_value in scope.split())


def should_send_oauth_resource(client_info: OAuthClientInformationFull | None) -> bool:
    if not client_info or not client_info.resource:
        return False

    mode = normalize_oauth_resource_parameter(client_info.oauth_resource_parameter)
    if mode == 'omit':
        return False
    if mode == 'include':
        return True

    return not scope_has_resource_indicator(client_info.scope)


def build_oauth_request_params(client_info: OAuthClientInformationFull | None) -> dict:
    if not client_info:
        return {}

    params = {}
    if client_info.scope:
        params['scope'] = client_info.scope
    if should_send_oauth_resource(client_info):
        params['resource'] = client_info.resource
    return params


async def recover_static_oauth_client_metadata(connection: dict, oauth_client_info: dict) -> dict:
    if connection.get('auth_type') != 'oauth_2.1_static':
        return oauth_client_info

    if oauth_client_info.get('scope') and oauth_client_info.get('resource'):
        return oauth_client_info

    server_url = connection.get('url')
    if not server_url:
        return oauth_client_info

    try:
        resource_metadata = await get_protected_resource_metadata(server_url)
    except Exception as e:
        log.debug('Unable to recover static OAuth metadata for %s: %s', server_url, e)
        return oauth_client_info

    recovered = {**oauth_client_info}
    if not recovered.get('scope') and resource_metadata.scopes_supported:
        recovered['scope'] = ' '.join(resource_metadata.scopes_supported)
        log.info('Recovered static OAuth scopes for %s from protected resource metadata', server_url)

    if not recovered.get('resource') and resource_metadata.resource:
        recovered['resource'] = resource_metadata.resource

    return recovered


class OAuthClientManager:
    def __init__(self, app):
        self.oauth = OAuth()
        self.app = app
        self.clients = {}

    def add_client(self, client_id, oauth_client_info: OAuthClientInformationFull):
        kwargs = {
            'name': client_id,
            'client_id': oauth_client_info.client_id,
            'client_secret': oauth_client_info.client_secret,
            'client_kwargs': {
                'follow_redirects': True,
                **({'timeout': int(OAUTH_CLIENT_TIMEOUT)} if OAUTH_CLIENT_TIMEOUT else {}),
                **({'scope': oauth_client_info.scope} if oauth_client_info.scope else {}),
                **(
                    {'token_endpoint_auth_method': oauth_client_info.token_endpoint_auth_method}
                    if oauth_client_info.token_endpoint_auth_method
                    else {}
                ),
            },
            'server_metadata_url': (oauth_client_info.issuer if oauth_client_info.issuer else None),
        }

        # Defense-in-depth: when the server metadata is already known, pass the
        # authorization/token endpoints explicitly so authlib does not rely solely
        # on refetching server_metadata_url (which may be missing/unreachable) to
        # resolve them. Prevents RuntimeError: Missing "authorize_url". (#26647)
        server_metadata = oauth_client_info.server_metadata
        if server_metadata is not None:
            if getattr(server_metadata, 'authorization_endpoint', None):
                kwargs['authorize_url'] = str(server_metadata.authorization_endpoint)
            if getattr(server_metadata, 'token_endpoint', None):
                kwargs['access_token_url'] = str(server_metadata.token_endpoint)

        # Default to S256 for OAuth 2.1 (PKCE is mandatory per RFC 9700)
        kwargs['code_challenge_method'] = 'S256'

        # Only remove PKCE if metadata explicitly excludes S256
        if (
            oauth_client_info.server_metadata
            and oauth_client_info.server_metadata.code_challenge_methods_supported
            and isinstance(
                oauth_client_info.server_metadata.code_challenge_methods_supported,
                list,
            )
            and 'S256' not in oauth_client_info.server_metadata.code_challenge_methods_supported
        ):
            del kwargs['code_challenge_method']

        self.clients[client_id] = {
            'client': self.oauth.register(**kwargs),
            'client_info': oauth_client_info,
        }
        return self.clients[client_id]

    async def ensure_client_from_config(self, client_id):
        """
        Lazy-load an OAuth client from the current TOOL_SERVER_CONNECTIONS
        config if it hasn't been registered on this node yet.
        """
        if client_id in self.clients:
            return self.clients[client_id]['client']

        try:
            connections = await Config.get('tool_server.connections', [])
        except Exception:
            connections = []

        for connection in connections or []:
            if connection.get('type', 'openapi') != 'mcp':
                continue
            if connection.get('auth_type', 'none') not in ('oauth_2.1', 'oauth_2.1_static'):
                continue

            server_id = (connection.get('info') or {}).get('id')
            if not server_id:
                continue

            expected_client_id = f'mcp:{server_id}'
            if client_id != expected_client_id:
                continue

            oauth_client_info = (connection.get('info') or {}).get('oauth_client_info', '')
            if not oauth_client_info:
                continue

            try:
                oauth_client_info = resolve_oauth_client_info(connection)
                oauth_client_info = await recover_static_oauth_client_metadata(connection, oauth_client_info)
                oauth_client_info = apply_connection_oauth_options(connection, oauth_client_info)
                return self.add_client(expected_client_id, OAuthClientInformationFull(**oauth_client_info))['client']
            except InvalidToken:
                log.error(
                    'Failed to lazily add OAuth client %s from config: InvalidToken. '
                    'Stored OAuth client data is invalid; reconnect this tool server.',
                    expected_client_id,
                )
                continue
            except Exception as e:
                log.error(
                    'Failed to lazily add OAuth client %s from config: %s',
                    expected_client_id,
                    f'{type(e).__name__}: {e}' if str(e) else type(e).__name__,
                )
                continue

        return None

    def remove_client(self, client_id):
        if client_id in self.clients:
            del self.clients[client_id]
            log.info('Removed OAuth client %s', client_id)

        if hasattr(self.oauth, '_clients'):
            if client_id in self.oauth._clients:
                self.oauth._clients.pop(client_id, None)

        if hasattr(self.oauth, '_registry'):
            if client_id in self.oauth._registry:
                self.oauth._registry.pop(client_id, None)

        return True

    async def _preflight_authorization_url(self, client, client_info: OAuthClientInformationFull) -> bool:
        # TODO: Replace this logic with a more robust OAuth client registration validation
        # Only perform preflight checks for Starlette OAuth clients
        if not hasattr(client, 'create_authorization_url'):
            return True

        redirect_uri = None
        if client_info.redirect_uris:
            redirect_uri = str(client_info.redirect_uris[0])

        try:
            kwargs = build_oauth_request_params(client_info)
            auth_data = await client.create_authorization_url(redirect_uri=redirect_uri, **kwargs)
            authorization_url = auth_data.get('url')

            if not authorization_url:
                return True
        except Exception as e:
            log.debug('Skipping OAuth preflight for client %s: %s', client_info.client_id, e)
            return True

        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(
                    authorization_url,
                    allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as resp:
                    if resp.status < 400:
                        return True
                    response_text = await resp.text()

                    error = None
                    error_description = ''

                    content_type = resp.headers.get('content-type', '')
                    if 'application/json' in content_type:
                        try:
                            payload = JSONCodec.loads(response_text)
                            error = payload.get('error')
                            error_description = payload.get('error_description', '')
                        except Exception:
                            pass
                    else:
                        error_description = response_text

                    error_message = f'{error or ""} {error_description or ""}'.lower()

                    if any(
                        keyword in error_message
                        for keyword in (
                            'invalid_client',
                            'invalid client',
                            'client id',
                            'redirect_uri',
                            'redirect uri',
                        )
                    ):
                        log.warning(
                            f'OAuth client preflight detected invalid registration for {client_info.client_id}: {error} {error_description}'
                        )

                        return False
        except Exception as e:
            log.debug('Skipping OAuth preflight network check for client %s: %s', client_info.client_id, e)

        return True

    async def get_client(self, client_id):
        if client_id not in self.clients:
            await self.ensure_client_from_config(client_id)

        client = self.clients.get(client_id)
        return client['client'] if client else None

    async def get_client_info(self, client_id):
        if client_id not in self.clients:
            await self.ensure_client_from_config(client_id)

        client = self.clients.get(client_id)
        return client['client_info'] if client else None

    async def get_server_metadata_url(self, client_id):
        client = await self.get_client(client_id)
        if not client:
            return None

        return client._server_metadata_url if hasattr(client, '_server_metadata_url') else None

    async def get_oauth_token(self, user_id: str, client_id: str, force_refresh: bool = False):
        """
        Get a valid OAuth token for the user, automatically refreshing if needed.

        Args:
            user_id: The user ID
            client_id: The OAuth client ID (provider)
            force_refresh: Force token refresh even if current token appears valid

        Returns:
            dict: OAuth token data with access_token, or None if no valid token available
        """
        try:
            # Get the OAuth session
            session = await OAuthSessions.get_session_by_provider_and_user_id(client_id, user_id)
            if not session:
                log.warning(f'No OAuth session found for user {user_id}, client_id {client_id}')
                return None

            if (
                force_refresh
                or session.expires_at is None
                or datetime.now() + timedelta(minutes=5) >= datetime.fromtimestamp(session.expires_at)
            ):
                log.debug('Token refresh needed for user %s, client_id %s', user_id, session.provider)
                refreshed_token = await self._refresh_token(session)
                if refreshed_token:
                    return refreshed_token
                else:
                    log.warning(
                        f'Token refresh failed for user {user_id}, client_id {session.provider}, deleting session {session.id}'
                    )
                    await OAuthSessions.delete_session_by_id(session.id)
                    return None
            return session.token

        except Exception as e:
            log.error(f'Error getting OAuth token for user {user_id}: {e}')
            return None

    async def _refresh_token(self, session) -> dict:
        """
        Refresh an OAuth token if needed, with concurrency protection.

        Args:
            session: The OAuth session object

        Returns:
            dict: Refreshed token data, or None if refresh failed
        """
        try:
            # Perform the actual refresh
            refreshed_token = await self._perform_token_refresh(session)

            if refreshed_token:
                # Update the session with new token data
                session = await OAuthSessions.update_session_by_id(session.id, refreshed_token)
                log.info('Successfully refreshed token for session %s', session.id)
                return session.token
            else:
                log.error(f'Failed to refresh token for session {session.id}')
                return None

        except Exception as e:
            log.error(f'Error refreshing token for session {session.id}: {e}')
            return None

    async def _perform_token_refresh(self, session) -> dict:
        """
        Perform the actual OAuth token refresh.

        Args:
            session: The OAuth session object

        Returns:
            dict: New token data, or None if refresh failed
        """
        auth_config = await get_oauth_runtime_config()
        client_id = session.provider
        token_data = session.token

        if not token_data.get('refresh_token'):
            log.warning(f'No refresh token available for session {session.id}')
            return None

        try:
            client = await self.get_client(client_id)
            if not client:
                log.error(f'No OAuth client found for provider {client_id}')
                return None

            token_endpoint = None
            async with aiohttp.ClientSession(trust_env=True) as session_http:
                async with session_http.get(await self.get_server_metadata_url(client_id)) as r:
                    if r.status == 200:
                        openid_data = await r.json()
                        token_endpoint = openid_data.get('token_endpoint')
                    else:
                        log.error(f'Failed to fetch OpenID configuration for client_id {client_id}')
            if not token_endpoint:
                log.error(f'No token endpoint found for client_id {client_id}')
                return None

            # Prepare refresh request
            refresh_data = {
                'grant_type': 'refresh_token',
                'refresh_token': token_data['refresh_token'],
                'client_id': client.client_id,
            }
            client_info = await self.get_client_info(client_id)
            if should_send_oauth_resource(client_info):
                refresh_data['resource'] = client_info.resource

            if hasattr(client, 'client_secret') and client.client_secret:
                refresh_data['client_secret'] = client.client_secret

            # Add scope if available in client kwargs (some providers require it on refresh)
            if (
                hasattr(client, 'client_kwargs')
                and client.client_kwargs.get('scope')
                and auth_config.OAUTH_REFRESH_TOKEN_INCLUDE_SCOPE
            ):
                refresh_data['scope'] = client.client_kwargs['scope']

            # Make refresh request
            async with aiohttp.ClientSession(trust_env=True) as session_http:
                async with session_http.post(
                    token_endpoint,
                    data=refresh_data,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    if r.status == 200:
                        new_token_data = await r.json()

                        # Merge with existing token data (preserve refresh_token if not provided)
                        if 'refresh_token' not in new_token_data:
                            new_token_data['refresh_token'] = token_data['refresh_token']

                        _normalize_token_expiry(new_token_data)

                        log.debug('Token refresh successful for client_id %s', client_id)
                        return new_token_data
                    else:
                        error_text = await r.text()
                        log.error(f'Token refresh failed for client_id {client_id}: {r.status} - {error_text}')
                        return None

        except Exception as e:
            log.error(f'Exception during token refresh for client_id {client_id}: {e}')
            return None

    async def handle_authorize(self, request, client_id: str, user_id: str) -> RedirectResponse:
        client = await self.get_client(client_id)
        if client is None:
            raise HTTPException(404)
        client_info = await self.get_client_info(client_id)
        if client_info is None:
            # get_client registers client_info too
            client_info = await self.get_client_info(client_id)
        if client_info is None:
            raise HTTPException(404)

        redirect_uri = client_info.redirect_uris[0] if client_info.redirect_uris else None
        redirect_uri_str = str(redirect_uri) if redirect_uri else None
        # Pass explicit scope/resource parameters for providers that require them.
        kwargs = build_oauth_request_params(client_info)
        try:
            auth_data = await client.create_authorization_url(redirect_uri_str, **kwargs)
            if not auth_data.get('state'):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail='OAuth authorization state was not generated',
                )
            auth_data['user_id'] = user_id
            await client.save_authorize_data(request, redirect_uri=redirect_uri_str, **auth_data)
            return RedirectResponse(auth_data['url'], status_code=302)
        except RuntimeError as e:
            # authlib raises RuntimeError('Missing "authorize_url" value') when the
            # authorization endpoint could not be resolved from server metadata.
            # Surface a clear 400 instead of an uncaught 500 for clients that were
            # registered before discovery was validated. (#26647)
            log.error(f'OAuth authorize failed for client {client_id}: {e}')
            raise HTTPException(
                status_code=400,
                detail=(
                    'OAuth authorization endpoint could not be resolved for this '
                    'client. Re-register the MCP server; its OAuth discovery '
                    'documents may be missing or unreachable.'
                ),
            )

    async def handle_callback(self, request, client_id: str, response):
        client = await self.get_client(client_id)
        if client is None:
            raise HTTPException(404)

        error_message = None
        state = request.query_params.get('state')
        user_id = None
        try:
            client_info = await self.get_client_info(client_id)
            state_data = await client.framework.get_state_data(request.session, state) if state else None
            user_id = state_data.get('user_id') if state_data else None
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='OAuth callback state is invalid or expired',
                )

            if not await get_verified_user_by_id(user_id):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail='OAuth callback user is not authorized',
                )

            request_user = await get_optional_verified_user_from_request(request)
            if request_user and request_user.id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail='OAuth callback user does not match authenticated session',
                )

            # Note: Do NOT pass client_id/client_secret explicitly here.
            # The Authlib client already has these configured during add_client().
            # Passing them again causes Authlib to concatenate them (e.g., "ID1,ID1"),
            # which results in 401 errors from the token endpoint. (Fix for #19823)
            token_kwargs = {}
            if should_send_oauth_resource(client_info):
                token_kwargs['resource'] = client_info.resource
            token = await client.authorize_access_token(request, **token_kwargs)

            # Validate that we received a proper token response
            # If token exchange failed (e.g., 401), we may get an error response instead
            if token and not token.get('access_token'):
                error_desc = token.get('error_description', token.get('error', 'Unknown error'))
                error_message = f'Token exchange failed: {error_desc}'
                log.error(f'Invalid token response for client_id {client_id}: {token}')
                token = None

            if token:
                try:
                    _normalize_token_expiry(token)

                    # Clean up any existing sessions for this user/client_id first
                    sessions = await OAuthSessions.get_sessions_by_user_id(user_id)
                    for session in sessions:
                        if session.provider == client_id:
                            await OAuthSessions.delete_session_by_id(session.id)

                    session = await OAuthSessions.create_session(
                        user_id=user_id,
                        provider=client_id,
                        token=token,
                    )
                    log.info('Stored OAuth session server-side for user %s, client_id %s', user_id, client_id)
                except Exception as e:
                    error_message = 'Failed to store OAuth session server-side'
                    log.error(f'Failed to store OAuth session server-side: {e}')
            else:
                if not error_message:
                    error_message = 'Failed to obtain OAuth token'
                log.warning(error_message)
        except Exception as e:
            error_message = _build_oauth_callback_error_message(e)
            log.warning(
                'OAuth callback error for user_id=%s client_id=%s: %s',
                user_id,
                client_id,
                error_message,
                exc_info=True,
            )
        finally:
            if state and client is not None:
                await client.framework.clear_state_data(request.session, state)

        webui_url = await Config.get('webui.url')
        redirect_url = (str(webui_url or request.base_url)).rstrip('/')

        if error_message:
            log.debug(error_message)
            redirect_url = f'{redirect_url}/?error={urllib.parse.quote_plus(error_message)}'
            return RedirectResponse(url=redirect_url, headers=response.headers)

        response = RedirectResponse(url=redirect_url, headers=response.headers)
        return response

