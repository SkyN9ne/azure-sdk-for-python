# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
import os
import time

from msal.application import PublicClientApplication

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from .. import CredentialUnavailableError
from .._constants import DEVELOPER_SIGN_ON_CLIENT_ID
from .._internal import AadClient, resolve_tenant, validate_tenant_id
from .._internal.decorators import log_get_token, wrap_exceptions
from .._internal.msal_client import MsalClient
from .._internal.shared_token_cache import NO_TOKEN, SharedTokenCacheBase

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    # pylint:disable=unused-import,ungrouped-imports
    from typing import Any, Dict, Optional
    from .. import AuthenticationRecord
    from .._internal import AadClientBase


class SharedTokenCacheCredential(SharedTokenCacheBase):
    """Authenticates using tokens in the local cache shared between Microsoft applications.

    :param str username:
        Username (typically an email address) of the user to authenticate as. This is used when the local cache
        contains tokens for multiple identities.

    :keyword str authority: Authority of an Azure Active Directory endpoint, for example 'login.microsoftonline.com',
        the authority for Azure Public Cloud (which is the default). :class:`~azure.identity.AzureAuthorityHosts`
        defines authorities for other clouds.
    :keyword str tenant_id: an Azure Active Directory tenant ID. Used to select an account when the cache contains
        tokens for multiple identities.
    :keyword AuthenticationRecord authentication_record: an authentication record returned by a user credential such as
        :class:`DeviceCodeCredential` or :class:`InteractiveBrowserCredential`
    :keyword cache_persistence_options: configuration for persistent token caching. If not provided, the credential
        will use the persistent cache shared by Microsoft development applications
    :paramtype cache_persistence_options: ~azure.identity.TokenCachePersistenceOptions
    :keyword bool allow_multitenant_authentication: when True, enables the credential to acquire tokens from any tenant
        the user is registered in. When False, which is the default, the credential will acquire tokens only from the
        user's home tenant or, if a value was given for **authentication_record**, the tenant specified by the
        :class:`AuthenticationRecord`.
    """

    def __init__(self, username=None, **kwargs):
        # type: (Optional[str], **Any) -> None

        self._auth_record = kwargs.pop("authentication_record", None)  # type: Optional[AuthenticationRecord]
        if self._auth_record:
            # authenticate in the tenant that produced the record unless "tenant_id" specifies another
            self._tenant_id = kwargs.pop("tenant_id", None) or self._auth_record.tenant_id
            validate_tenant_id(self._tenant_id)
            self._allow_multitenant = kwargs.pop("allow_multitenant_authentication", False)
            self._cache = kwargs.pop("_cache", None)
            self._client_applications = {}  # type: Dict[str, PublicClientApplication]
            self._msal_client = MsalClient(**kwargs)
            self._initialized = False
        else:
            super(SharedTokenCacheCredential, self).__init__(username=username, **kwargs)

    @log_get_token("SharedTokenCacheCredential")
    def get_token(self, *scopes, **kwargs):  # pylint:disable=unused-argument
        # type (*str, **Any) -> AccessToken
        """Get an access token for `scopes` from the shared cache.

        If no access token is cached, attempt to acquire one using a cached refresh token.

        This method is called automatically by Azure SDK clients.

        :param str scopes: desired scopes for the access token. This method requires at least one scope.
        :keyword str claims: additional claims required in the token, such as those returned in a resource provider's
          claims challenge following an authorization failure
        :rtype: :class:`azure.core.credentials.AccessToken`
        :raises ~azure.identity.CredentialUnavailableError: the cache is unavailable or contains insufficient user
            information
        :raises ~azure.core.exceptions.ClientAuthenticationError: authentication failed. The error's ``message``
          attribute gives a reason.
        """
        if not scopes:
            raise ValueError("'get_token' requires at least one scope")

        if not self._initialized:
            self._initialize()

        if not self._cache:
            raise CredentialUnavailableError(message="Shared token cache unavailable")

        if self._auth_record:
            return self._acquire_token_silent(*scopes, **kwargs)

        account = self._get_account(self._username, self._tenant_id)

        token = self._get_cached_access_token(scopes, account)
        if token:
            return token

        # try each refresh token, returning the first access token acquired
        for refresh_token in self._get_refresh_tokens(account):
            token = self._client.obtain_token_by_refresh_token(scopes, refresh_token, **kwargs)
            return token

        raise CredentialUnavailableError(message=NO_TOKEN.format(account.get("username")))

    def _get_auth_client(self, **kwargs):
        # type: (**Any) -> AadClientBase
        return AadClient(client_id=DEVELOPER_SIGN_ON_CLIENT_ID, **kwargs)

    def _initialize(self):
        if self._initialized:
            return

        if not self._auth_record:
            super(SharedTokenCacheCredential, self)._initialize()
            return

        self._load_cache()
        self._initialized = True

    def _get_client_application(self, **kwargs):
        tenant_id = resolve_tenant(self._tenant_id, self._allow_multitenant, **kwargs)
        if tenant_id not in self._client_applications:
            # CP1 = can handle claims challenges (CAE)
            capabilities = None if "AZURE_IDENTITY_DISABLE_CP1" in os.environ else ["CP1"]
            self._client_applications[tenant_id] = PublicClientApplication(
                client_id=self._auth_record.client_id,
                authority="https://{}/{}".format(self._auth_record.authority, tenant_id),
                token_cache=self._cache,
                http_client=self._msal_client,
                client_capabilities=capabilities
            )
        return self._client_applications[tenant_id]

    @wrap_exceptions
    def _acquire_token_silent(self, *scopes, **kwargs):
        # type: (*str, **Any) -> AccessToken
        """Silently acquire a token from MSAL. Requires an AuthenticationRecord."""

        # this won't be None when this method is called by get_token but we check anyway to satisfy mypy
        if self._auth_record is None:
            raise CredentialUnavailableError("Initialization failed")

        result = None

        client_application = self._get_client_application(**kwargs)
        accounts_for_user = client_application.get_accounts(username=self._auth_record.username)
        if not accounts_for_user:
            raise CredentialUnavailableError("The cache contains no account matching the given AuthenticationRecord.")

        for account in accounts_for_user:
            if account.get("home_account_id") != self._auth_record.home_account_id:
                continue

            now = int(time.time())
            result = client_application.acquire_token_silent_with_error(
                list(scopes), account=account, claims_challenge=kwargs.get("claims")
            )
            if result and "access_token" in result and "expires_in" in result:
                return AccessToken(result["access_token"], now + int(result["expires_in"]))

        # if we get this far, the cache contained a matching account but MSAL failed to authenticate it silently
        if result:
            # cache contains a matching refresh token but STS returned an error response when MSAL tried to use it
            message = "Token acquisition failed"
            details = result.get("error_description") or result.get("error")
            if details:
                message += ": {}".format(details)
            raise ClientAuthenticationError(message=message)

        # cache doesn't contain a matching refresh (or access) token
        raise CredentialUnavailableError(message=NO_TOKEN.format(self._auth_record.username))
