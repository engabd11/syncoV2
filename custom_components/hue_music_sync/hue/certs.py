"""Signify's private root CAs for Hue bridges.

From the Hue "Using HTTPS" guide: *"Signify has issued two private root CA
Certificates used for Hue Bridges: one that is currently in use, and one
secondary which we may switch to in the future. To use HTTPS correctly, the
client must validate the Hue Bridge certificate against these root CA
certificates."* The guide is equally blunt about the alternative: disabling
validation *"can be done for development/testing purposes, but must never be
used in production"*.

Two things beyond the CA chain matter:

* **Common name.** The bridge certificate's subject CN is the *bridge id*, not
  its IP address, so ordinary hostname verification can never pass. The
  documented approach is to check the CN equals the bridge id yourself — see
  ``cert_common_name`` and its caller in ``__init__.py``.
* **Intermediates.** Today bridge certificates are signed directly by the root,
  but the guide warns that a future switch to the secondary root will likely
  introduce an intermediate CA. Only the roots need bundling (the bridge
  presents its own chain), but the client must support chains — which
  ``ssl.create_default_context(cadata=...)`` does.

Bridges old enough to still carry a self-signed certificate exist; the guide's
advice there is to update the firmware. The caller falls back to
trust-on-first-use pinning for those rather than refusing to work.
"""

from __future__ import annotations

# Currently active root ("Philips Hue" / CN=root-bridge).
HUE_ROOT_CA_ACTIVE = """-----BEGIN CERTIFICATE-----
MIICMjCCAdigAwIBAgIUO7FSLbaxikuXAljzVaurLXWmFw4wCgYIKoZIzj0EAwIw
OTELMAkGA1UEBhMCTkwxFDASBgNVBAoMC1BoaWxpcHMgSHVlMRQwEgYDVQQDDAty
b290LWJyaWRnZTAiGA8yMDE3MDEwMTAwMDAwMFoYDzIwMzgwMTE5MDMxNDA3WjA5
MQswCQYDVQQGEwJOTDEUMBIGA1UECgwLUGhpbGlwcyBIdWUxFDASBgNVBAMMC3Jv
b3QtYnJpZGdlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjNw2tx2AplOf9x86
aTdvEcL1FU65QDxziKvBpW9XXSIcibAeQiKxegpq8Exbr9v6LBnYbna2VcaK0G22
jOKkTqOBuTCBtjAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBhjAdBgNV
HQ4EFgQUZ2ONTFrDT6o8ItRnKfqWKnHFGmQwdAYDVR0jBG0wa4AUZ2ONTFrDT6o8
ItRnKfqWKnHFGmShPaQ7MDkxCzAJBgNVBAYTAk5MMRQwEgYDVQQKDAtQaGlsaXBz
IEh1ZTEUMBIGA1UEAwwLcm9vdC1icmlkZ2WCFDuxUi22sYpLlwJY81Wrqy11phcO
MAoGCCqGSM49BAMCA0gAMEUCIEBYYEOsa07TH7E5MJnGw557lVkORgit2Rm1h3B2
sFgDAiEA1Fj/C3AN5psFMjo0//mrQebo0eKd3aWRx+pQY08mk48=
-----END CERTIFICATE-----
"""

# Secondary root ("Signify Hue" / CN=Hue Root CA 01), not yet in use.
HUE_ROOT_CA_SECONDARY = """-----BEGIN CERTIFICATE-----
MIIBzDCCAXOgAwIBAgICEAAwCgYIKoZIzj0EAwIwPDELMAkGA1UEBhMCTkwxFDAS
BgNVBAoMC1NpZ25pZnkgSHVlMRcwFQYDVQQDDA5IdWUgUm9vdCBDQSAwMTAgFw0y
NTAyMjUwMDAwMDBaGA8yMDUwMTIzMTIzNTk1OVowPDELMAkGA1UEBhMCTkwxFDAS
BgNVBAoMC1NpZ25pZnkgSHVlMRcwFQYDVQQDDA5IdWUgUm9vdCBDQSAwMTBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABFfOO0jfSAUXGQ9kjEDzyBrcMQ3ItyA5krE+
cyvb1Y3xFti7KlAad8UOnAx0FBLn7HZrlmIwm1QnX0fK3LPM13mjYzBhMB0GA1Ud
DgQWBBTF1pSpsCASX/z0VHLigxU2CAaqoTAfBgNVHSMEGDAWgBTF1pSpsCASX/z0
VHLigxU2CAaqoTAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBBjAKBggq
hkjOPQQDAgNHADBEAiAk7duT+IHbOGO4UUuGLAEpyYejGZK9Z7V9oSfnvuQ5BQIg
IYSgwwxHXm73/JgcU9lAM6c8Bmu3UE3kBIUwBs1qXFw=
-----END CERTIFICATE-----
"""

HUE_ROOT_CA_BUNDLE = HUE_ROOT_CA_ACTIVE + HUE_ROOT_CA_SECONDARY


def cert_common_name(pem: str) -> str | None:
    """Read the subject Common Name out of a PEM certificate.

    Used to check the bridge certificate's CN against the bridge id, which is
    the documented substitute for hostname verification (the certificate never
    carries the bridge's IP address). Returns None if the certificate cannot be
    parsed, so the caller can decide how strict to be.
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        cert = x509.load_pem_x509_certificate(pem.encode())
        names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    except Exception:  # noqa: BLE001 - a malformed cert is the caller's problem
        return None
    if not names:
        return None
    value = names[0].value
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def looks_like_bridge_id(value: str | None) -> bool:
    """Whether a string has the shape of a Hue bridge id (16 hex characters).

    Lets the caller distinguish "this certificate belongs to a *different*
    bridge" — which must be refused — from "this certificate has a subject we
    don't recognise", which is only worth a warning as long as the chain still
    validates against Signify's roots.
    """
    if not value:
        return False
    candidate = value.strip()
    return len(candidate) == 16 and all(c in "0123456789abcdefABCDEF" for c in candidate)


def matches_bridge_id(common_name: str | None, bridge_id: str | None) -> bool:
    """Whether a certificate CN identifies the bridge we think we're talking to.

    Bridge ids are hex and appear upper- or lower-cased depending on which
    endpoint returned them, so the comparison is case-insensitive.
    """
    if not common_name or not bridge_id:
        return False
    return common_name.strip().lower() == bridge_id.strip().lower()
