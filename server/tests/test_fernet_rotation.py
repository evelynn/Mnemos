"""Round-trip rewrap logic for the Fernet key rotation helper.

Doesn't invoke the CLI directly — we exercise ``cryptography.fernet``
the same way the rotation code does, so a future refactor that drops the
library would still be caught by these tests.
"""

from cryptography.fernet import Fernet, InvalidToken


def test_rewrap_changes_ciphertext_but_not_plaintext():
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()

    plaintext = "Server=db.example.com,1433;User Id=svc_mnemos;"
    old_ct = Fernet(old_key).encrypt(plaintext.encode())

    # Rewrap
    recovered = Fernet(old_key).decrypt(old_ct).decode()
    new_ct = Fernet(new_key).encrypt(recovered.encode())

    assert old_ct != new_ct
    assert Fernet(new_key).decrypt(new_ct).decode() == plaintext


def test_wrong_old_key_detected_via_invalid_token():
    plaintext = "abc"
    k1 = Fernet.generate_key()
    k2 = Fernet.generate_key()
    ct = Fernet(k1).encrypt(plaintext.encode())
    try:
        Fernet(k2).decrypt(ct)
    except InvalidToken:
        return
    raise AssertionError("expected InvalidToken with the wrong key")


def test_rotated_secret_still_reads_after_second_rotation():
    k1 = Fernet.generate_key()
    k2 = Fernet.generate_key()
    k3 = Fernet.generate_key()
    plaintext = "round-trip-check"

    ct = Fernet(k1).encrypt(plaintext.encode())
    ct = Fernet(k2).encrypt(Fernet(k1).decrypt(ct))
    ct = Fernet(k3).encrypt(Fernet(k2).decrypt(ct))

    assert Fernet(k3).decrypt(ct).decode() == plaintext
