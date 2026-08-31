"""Where a Lambda gets its configuration.

Two sources, deliberately, because two kinds of value are involved. Names like
the table and the queue URL are not secret and arrive as environment variables.
Client secrets and the webhook token are SecureStrings in SSM Parameter Store and
are read **at run time**, not baked into the function's environment.

That split is a security decision, not a style one. Resolving a SecureString in
Terraform and passing it as an environment variable would write the plaintext
into Terraform state *and* into the function's configuration, where anyone with
`lambda:GetFunction` can read it back. Reading at run time keeps it in SSM behind
KMS, and costs one call per cold start because the value is cached for the life
of the container.

`PLAN.md` §9 specifies SSM SecureString and leaves the delivery open; this is that
choice, and `config.py` takes its secrets through a callable so it can be changed
without touching a handler.
"""

import os
from collections.abc import Callable
from typing import Any

import boto3

# Parameters live under one path so a single IAM statement can grant the whole
# set: /reckon/google_client_id and so on.
DEFAULT_PREFIX = "/reckon"

# Environment variable names are the same ones the CLI uses, so a Lambda and a
# laptop are configured identically. The SSM parameter name is derived, rather
# than configured separately, so the two cannot drift apart.
_ENV_PREFIX = "RECKON_"


def parameter_name(variable: str, prefix: str = DEFAULT_PREFIX) -> str:
    """`RECKON_GOOGLE_CLIENT_ID` -> `/reckon/google_client_id`."""
    return f"{prefix}/{variable.removeprefix(_ENV_PREFIX).lower()}"


class Secrets:
    """Environment first, then SSM. Cached for the life of the container."""

    def __init__(
        self,
        *,
        prefix: str = DEFAULT_PREFIX,
        client: Any = None,
        environ: Callable[[str], str | None] = os.environ.get,
    ) -> None:
        self.prefix = prefix
        self._injected = client
        self._environ = environ
        self._cache: dict[str, str] = {}

    @property
    def client(self) -> Any:
        if self._injected is None:
            self._injected = boto3.client("ssm")
        return self._injected

    def __call__(self, variable: str) -> str:
        """The value for one configuration name, or raise naming what is missing."""
        if (value := self._environ(variable)) is not None:
            return value
        if variable in self._cache:
            return self._cache[variable]

        name = parameter_name(variable, self.prefix)
        try:
            response = self.client.get_parameter(Name=name, WithDecryption=True)
        except Exception as exc:
            raise KeyError(
                f"{variable} is not in the environment and {name} could not be read from SSM: {exc}"
            ) from exc
        self._cache[variable] = response["Parameter"]["Value"]
        return self._cache[variable]
