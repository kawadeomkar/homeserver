"""Documentation of the connection options every module in this role takes.

They are module_utils/opnsense_api.py's api_argument_spec(). A module lists the fragment in
extends_documentation_fragment: opnsense_api. tests/policy/test_roles.py checks that each module's documentation,
with these merged in, matches its argument spec.
"""


class ModuleDocFragment:
    DOCUMENTATION = r"""
options:
  api_host:
    description:
      - The router's address.
      - Required, but checked in code rather than marked required, so the role can supply it through
        C(module_defaults).
    type: str
    default: ""
  api_port:
    description: HTTPS port of the router's web server.
    type: int
    default: 443
  api_key:
    description:
      - The key of an OPNsense API key pair. Keep it in an ansible-vault file.
      - Required, but checked in code rather than marked required, so the role can supply it through
        C(module_defaults).
    type: str
    default: ""
  api_secret:
    description:
      - The secret of the same API key pair. Keep it in an ansible-vault file.
      - Required, but checked in code rather than marked required, so the role can supply it through
        C(module_defaults).
    type: str
    default: ""
  validate_certs:
    description:
      - Verify the router's TLS certificate. A default install's certificate is self-signed, so
        O(api_cert_sha256) pins it instead.
    type: bool
    default: false
  api_cert_sha256:
    description:
      - SHA-256 fingerprint of the router's web certificate, colons optional. The key and secret are sent only to a
        server presenting it. Required while O(validate_certs) is off.
    type: str
    default: ""
  api_timeout:
    description: Seconds to wait for the connection and for each answer.
    type: int
    default: 60
"""
