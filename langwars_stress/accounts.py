from __future__ import annotations

import getpass
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .http import Client, Response, query
from .util import eprint, private_file, slug_for_url, utc_now, write_json_atomic


class ProvisionError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class RateLimited(ProvisionError):
    def __init__(self, wait: float, code: str | None = None):
        super().__init__(f"rate limited for {wait:.1f}s")
        self.wait = wait
        self.code = code


class AuthError(ProvisionError):
    pass


@dataclass
class Account:
    username: str
    password: str
    token: str | None = None
    user_id: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "password": self.password,
            "token": self.token,
            "userId": self.user_id,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "Account":
        return cls(
            username=str(value["username"]),
            password=str(value["password"]),
            token=value.get("token"),
            user_id=value.get("userId"),
            created_at=value.get("createdAt"),
        )


class AccountStore:
    def __init__(self, path: Path, base_url: str):
        self.path = path
        self.base_url = base_url.rstrip("/")

    def load(self) -> list[Account]:
        if not self.path.exists():
            return []
        import json

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            raise ProvisionError(f"cannot read account file {self.path}: {error}") from error
        stored_url = str(data.get("baseUrl", "")).rstrip("/")
        if stored_url and stored_url != self.base_url:
            raise ProvisionError(
                f"account file {self.path} belongs to {stored_url}, not {self.base_url}; "
                "use --accounts with a different file"
            )
        accounts = data.get("accounts")
        if not isinstance(accounts, list):
            raise ProvisionError(f"account file {self.path} is malformed")
        return [Account.from_dict(item) for item in accounts]

    def save(self, accounts: list[Account]) -> None:
        write_json_atomic(self.path, {
            "version": 1,
            "baseUrl": self.base_url,
            "updatedAt": utc_now(),
            "notice": "Test session tokens and passwords. Keep this file out of version control.",
            "accounts": [account.to_dict() for account in accounts],
        })
        private_file(self.path)


def default_accounts_path(base_url: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "state" / f"accounts-{slug_for_url(base_url)}.json"


def create_client(base_url: str, *, timeout: float, insecure: bool, ca_bundle: str | None, recorder=None) -> Client:
    return Client(base_url, timeout=timeout, insecure=insecure, ca_bundle=ca_bundle, recorder=recorder)


def _rate_limit(response: Response, fallback: float) -> RateLimited:
    return RateLimited(response.retry_after() or fallback, response.code)


def login_account(client: Client, account: Account, *, record: bool = False, timeout: float = 30.0) -> Response:
    response = client.request(
        "POST /api/auth/login",
        "POST",
        "/api/auth/login",
        body={"username": account.username, "password": account.password},
        timeout=timeout,
        record=record,
    )
    if response.status == 200 and isinstance(response.payload, dict):
        account.token = response.payload.get("token")
        user = response.payload.get("user") or {}
        account.user_id = user.get("id") or account.user_id
    elif response.status == 429:
        raise _rate_limit(response, 2.0)
    elif response.status == 401:
        raise AuthError("wrong username or password")
    elif response.status == 0:
        raise ProvisionError(f"transport error during login: {response.transport_error}")
    else:
        raise ProvisionError(f"login returned HTTP {response.status} ({response.code or 'no code'})")
    return response


def admin_login(client: Client, username: str, password: str, *, timeout: float = 30.0) -> tuple[str, dict]:
    response = client.request(
        "POST /api/auth/login",
        "POST",
        "/api/auth/login",
        body={"username": username, "password": password},
        timeout=timeout,
        record=False,
    )
    if response.status == 200 and isinstance(response.payload, dict):
        return response.payload["token"], response.payload.get("user") or {}
    if response.status == 401:
        raise AuthError("administrator login was rejected")
    if response.status == 429:
        raise RateLimited(response.retry_after() or 5.0, response.code)
    raise ProvisionError(f"administrator login returned HTTP {response.status} ({response.code or 'no code'})")


def read_admin_password(password_file: str | None) -> str:
    if password_file:
        try:
            return Path(password_file).read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError) as error:
            raise ProvisionError(f"cannot read admin password file: {error}") from error
    value = os.environ.get("LANGSWARS_ADMIN_PASSWORD")
    if value:
        return value
    if not os.isatty(0):
        raise ProvisionError(
            "administrator password is required; set LANGSWARS_ADMIN_PASSWORD, "
            "use --admin-password-file, or run interactively"
        )
    return getpass.getpass("LangWars administrator password: ")


def _create_invite(client: Client, token: str, uses: int, *, timeout: float) -> str:
    response = client.request(
        "POST /api/admin/invites",
        "POST",
        "/api/admin/invites",
        body={"maxUses": min(500, max(1, uses)), "expiresInHours": 168},
        token=token,
        timeout=timeout,
        record=False,
    )
    if response.status == 201 and isinstance(response.payload, dict) and response.payload.get("code"):
        return response.payload["code"]
    if response.status == 429:
        raise _rate_limit(response, 5.0)
    if response.status in (401, 403):
        raise AuthError("administrator session no longer has access")
    raise ProvisionError(f"creating an invitation returned HTTP {response.status} ({response.code or 'no code'})")


def _register_one(
    client: Client,
    username: str,
    password: str,
    invite_code: str | None,
    *,
    timeout: float,
) -> Account:
    body = {"username": username, "password": password}
    if invite_code:
        body["inviteCode"] = invite_code
    response = client.request(
        "POST /api/auth/register",
        "POST",
        "/api/auth/register",
        body=body,
        timeout=timeout,
        record=False,
    )
    if response.status == 201 and isinstance(response.payload, dict):
        account = Account(username=username, password=password, created_at=utc_now())
        account.token = response.payload.get("token")
        account.user_id = (response.payload.get("user") or {}).get("id")
        return account
    if response.status == 409:
        raise ProvisionError("username is already taken")
    if response.status == 429:
        raise _rate_limit(response, 1.0)
    if response.status == 403:
        raise ProvisionError(
            f"registration refused: {response.message or response.code or 'forbidden'}",
            code=response.code,
        )
    if response.status == 401:
        raise ProvisionError("registration unexpectedly required authentication")
    raise ProvisionError(
        f"registration returned HTTP {response.status} ({response.code or 'no code'}): "
        f"{response.message or response.text[:200]}"
    )


def _username(prefix: str, index: int, attempt: int = 0) -> str:
    suffix = "" if attempt == 0 else f"_{attempt}"
    value = f"{prefix}{index:04d}{suffix}"
    return value[:24]


def provision_accounts(
    *,
    base_url: str,
    path: Path,
    count: int,
    prefix: str,
    timeout: float,
    insecure: bool,
    ca_bundle: str | None,
    invite_code: str | None,
    admin_username: str | None,
    admin_password_file: str | None,
    min_interval: float,
    skip_verify: bool,
) -> list[Account]:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,12}", prefix):
        raise ProvisionError("--prefix must contain 1-12 ASCII letters, digits or underscores")
    store = AccountStore(path, base_url)
    accounts = store.load()
    client = create_client(base_url, timeout=timeout, insecure=insecure, ca_bundle=ca_bundle)

    config = client.request("SETUP /api/config", "GET", "/api/config", timeout=timeout, record=False)
    if not config.ok or not isinstance(config.payload, dict):
        raise ProvisionError(
            f"cannot read /api/config from {base_url}: HTTP {config.status} "
            f"{config.transport_error or config.code or ''}"
        )
    mode = config.payload.get("registrationMode", "invite")
    print(f"target: {base_url}")
    print(f"registration mode: {mode}")
    print(f"account file: {path}")

    admin_token: str | None = None
    invite_codes: list[str] = []
    invite_index = 0
    invite_remaining = 0
    supplied_invite = bool(invite_code)

    def ensure_admin() -> str:
        nonlocal admin_token
        if admin_token:
            return admin_token
        username = admin_username or os.environ.get("LANGSWARS_ADMIN_USERNAME") or "admin"
        password = read_admin_password(admin_password_file)
        delay = 1.0
        for _attempt in range(8):
            try:
                admin_token, _user = admin_login(client, username, password, timeout=timeout)
                return admin_token
            except RateLimited as error:
                wait = min(60.0, max(1.0, error.wait or delay))
                print(f"administrator login is throttled; waiting {wait:.0f}s")
                time.sleep(wait)
                delay = min(60.0, delay * 2)
        raise ProvisionError("administrator login remained rate limited")

    def next_invite() -> str:
        nonlocal invite_index, invite_remaining, supplied_invite
        if supplied_invite and invite_code:
            return invite_code
        if invite_codes and invite_index < len(invite_codes) and invite_remaining > 0:
            invite_remaining -= 1
            return invite_codes[invite_index]
        token = ensure_admin()
        code = _create_invite(client, token, max(50, count * 2), timeout=timeout)
        invite_codes.append(code)
        invite_index = len(invite_codes) - 1
        invite_remaining = max(50, count * 2) - 1
        return code

    if mode == "closed":
        raise ProvisionError("registration is closed on this deployment")
    if mode == "invite" and not invite_code:
        ensure_admin()  # fail early with a clear error if credentials are unusable

    # Refresh or repair existing accounts before appending new ones.
    if not skip_verify:
        print(f"verifying {len(accounts)} existing account(s)...")
        for position, account in enumerate(accounts, 1):
            if position % 25 == 0 or position == len(accounts):
                print(f"  verified {position}/{len(accounts)}")
            if account.token:
                response = client.request("SETUP /api/me", "GET", "/api/me", token=account.token, timeout=timeout, record=False)
                if response.status == 200 and isinstance(response.payload, dict):
                    account.user_id = response.payload.get("id") or account.user_id
                    continue
                if response.status not in (401, 429):
                    raise ProvisionError(f"unexpected session check HTTP {response.status} for {account.username}")
            for attempt in range(6):
                try:
                    login_account(client, account, timeout=timeout)
                    break
                except AuthError as error:
                    raise ProvisionError(
                        f"stored account {account.username} cannot log in ({error}); "
                        "it may have been deleted or its password reset"
                    ) from error
                except RateLimited as error:
                    if attempt == 5:
                        raise
                    wait = min(60.0, max(1.0, error.wait))
                    print(f"  login throttled; waiting {wait:.0f}s")
                    time.sleep(wait)
        store.save(accounts)

    interval = max(0.0, min_interval)
    attempts = 0
    collision = 0
    while len(accounts) < count:
        index = len(accounts)
        username = _username(prefix, index, collision)
        password = secrets.token_urlsafe(24)
        attempts += 1
        try:
            code = None
            if mode == "invite":
                code = next_invite()
            if interval:
                time.sleep(interval)
            account = _register_one(client, username, password, code, timeout=timeout)
            accounts.append(account)
            store.save(accounts)
            if len(accounts) % 10 == 0 or len(accounts) == count:
                print(f"provisioned {len(accounts)}/{count}")
            attempts = 0
            collision = 0
        except ProvisionError as error:
            message = str(error)
            error_code = getattr(error, "code", None)
            if "username is already taken" in message:
                # A lost state file can leave same-prefix accounts behind. Move to
                # a new suffix instead of looping on the same name forever.
                collision += 1
                if collision > 50:
                    raise ProvisionError(f"could not find a free generated username near {prefix}{index:04d}") from error
                print(f"username collision for {username}; trying another suffix")
                attempts = 0
                continue
            if isinstance(error, RateLimited):
                wait = min(60.0, max(1.0, error.wait))
                if interval < 1.1 and error.wait <= 1.0:
                    interval = 1.1
                    print("authentication rate limit detected; increasing spacing to 1.1s")
                print(f"registration throttled ({error.code or 'rate limit'}); waiting {wait:.0f}s")
                time.sleep(wait)
                attempts += 1
            elif error_code == "INVALID_INVITE":
                # The invitation may have expired or was consumed by someone else.
                print("invitation refused; requesting a new invitation")
                supplied_invite = False
                invite_codes.clear()
                invite_index = 0
                invite_remaining = 0
                attempts += 1
            elif error_code == "REGISTRATION_CLOSED":
                raise ProvisionError("registration is closed on this deployment") from error
            else:
                raise
            if attempts >= 12:
                raise ProvisionError(f"giving up on {username}: {message}") from error
    store.save(accounts)
    client.close()
    print(f"ready: {len(accounts)} account(s) in {path}")
    return accounts


def preflight_accounts(
    accounts: list[Account],
    *,
    base_url: str,
    timeout: float,
    insecure: bool,
    ca_bundle: str | None,
    limit: int | None = None,
) -> list[Account]:
    """Verify stored sessions without generating load metrics.

    Verification is concurrent because GET /api/me is cheap. Invalid sessions are
    refreshed sequentially so a mass token expiry does not create a login storm.
    """
    selected = accounts[:limit] if limit else list(accounts)
    if not selected:
        return []
    valid: list[Account] = []
    invalid: list[Account] = []
    from concurrent.futures import ThreadPoolExecutor

    def check(account: Account) -> bool:
        client = create_client(base_url, timeout=timeout, insecure=insecure, ca_bundle=ca_bundle)
        try:
            if not account.token:
                return False
            response = client.request("SETUP /api/me", "GET", "/api/me", token=account.token, timeout=timeout, record=False)
            if response.status == 200 and isinstance(response.payload, dict):
                account.user_id = response.payload.get("id") or account.user_id
                return True
            return False
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=min(16, max(1, len(selected)))) as pool:
        results = list(pool.map(check, selected))
    for account, ok in zip(selected, results):
        (valid if ok else invalid).append(account)

    if invalid:
        print(f"refreshing {len(invalid)} expired or missing session(s)...")
        client = create_client(base_url, timeout=timeout, insecure=insecure, ca_bundle=ca_bundle)
        refreshed: list[Account] = []
        for position, account in enumerate(invalid, 1):
            delay = 1.0
            for attempt in range(6):
                try:
                    login_account(client, account, timeout=timeout)
                    refreshed.append(account)
                    break
                except RateLimited as error:
                    wait = min(30.0, max(1.0, error.wait or delay))
                    print(f"  login throttled; waiting {wait:.0f}s")
                    time.sleep(wait)
                    delay = min(30.0, delay * 2)
                except AuthError as error:
                    print(f"  cannot refresh {account.username}: {error}")
                    break
                except ProvisionError as error:
                    print(f"  cannot refresh {account.username}: {error}")
                    break
            if position % 25 == 0:
                print(f"  refreshed {position}/{len(invalid)}")
        client.close()
        valid.extend(refreshed)
    order = {id(account): index for index, account in enumerate(selected)}
    valid.sort(key=lambda account: order.get(id(account), len(order)))
    return valid


def cleanup_accounts(
    *,
    base_url: str,
    path: Path,
    timeout: float,
    insecure: bool,
    ca_bundle: str | None,
    admin_username: str | None,
    admin_password_file: str | None,
) -> tuple[int, int]:
    store = AccountStore(path, base_url)
    accounts = store.load()
    if not accounts:
        print("no accounts in the state file")
        return 0, 0
    client = create_client(base_url, timeout=timeout, insecure=insecure, ca_bundle=ca_bundle)
    username = admin_username or os.environ.get("LANGSWARS_ADMIN_USERNAME") or "admin"
    password = read_admin_password(admin_password_file)
    token, _ = admin_login(client, username, password, timeout=timeout)
    listing = client.request("SETUP /api/admin/users", "GET", "/api/admin/users", token=token, timeout=timeout, record=False)
    if listing.status != 200 or not isinstance(listing.payload, list):
        raise ProvisionError(f"cannot list users: HTTP {listing.status}")
    by_name = {item.get("username"): item.get("id") for item in listing.payload if isinstance(item, dict)}
    deleted = missing = 0
    remaining: list[Account] = []
    for account in accounts:
        user_id = account.user_id or by_name.get(account.username)
        if not user_id:
            missing += 1
            continue
        response = client.request(
            "DELETE /api/admin/users/{id}",
            "DELETE",
            "/api/admin/users/" + user_id,
            token=token,
            timeout=timeout,
            record=False,
        )
        if response.status == 204:
            deleted += 1
        elif response.status == 404:
            missing += 1
        elif response.status == 409:
            print(f"cannot delete {account.username}: {response.message or response.code}")
            remaining.append(account)
        elif response.status in (401, 403):
            raise AuthError("administrator session lost access during cleanup")
        else:
            print(f"delete {account.username} returned HTTP {response.status}")
            remaining.append(account)
    store.save(remaining)
    client.close()
    return deleted, missing
