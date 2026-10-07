"""Documentation of the connection and timing options every module in this role takes.

They are module_utils/truenas_api.py's api_argument_spec(). A module lists the fragment in
extends_documentation_fragment: truenas_api for the options all modules share, and truenas_api.host for
api_host, which truenas_info replaces with a list of addresses.
"""


class ModuleDocFragment:
    DOCUMENTATION = r"""
options:
  api_port:
    description: HTTPS port of the TrueNAS web server.
    type: int
    default: 443
  api_key:
    description:
      - TrueNAS API key. Keep it in an ansible-vault file.
      - Required, but checked in code rather than marked required, so the role can supply it through
        C(module_defaults).
    type: str
    default: ""
  validate_certs:
    description:
      - Verify the NAS's TLS certificate. A default install's certificate is self-signed, so
        O(api_cert_sha256) pins it instead.
    type: bool
    default: false
  api_cert_sha256:
    description:
      - SHA-256 fingerprint of the NAS's certificate, in either case, with or without colons. When set,
        the API key is sent only to a server presenting that certificate.
    type: str
    default: ""
  api_timeout:
    description: Seconds to wait for each API answer.
    type: int
    default: 120
  api_connect_wait:
    description: Seconds to keep retrying a refused connection.
    type: int
    default: 60
  api_job_timeout:
    description: Seconds to wait for a job, such as a pool import, to finish.
    type: int
    default: 1800
  api_login_wait:
    description: Seconds to wait out TrueNAS's login rate limit, 20 logins per minute per client.
    type: int
    default: 75
"""

    HOST = r"""
options:
  api_host:
    description:
      - Address of the NAS.
      - Required, but checked in code rather than marked required, so the role can supply it through
        C(module_defaults).
    type: str
    default: ""
"""
