import pytest
from app.tokens import sign, verify, InvalidToken

SECRET_A = "a" * 32
SECRET_B = "b" * 32

def test_sign_and_verify_with_primary():
    tok = sign(123, "confirm", primary=SECRET_A, previous="")
    assert verify(tok, "confirm", primary=SECRET_A, previous="") == 123

def test_verify_fails_on_wrong_purpose():
    tok = sign(123, "confirm", primary=SECRET_A, previous="")
    with pytest.raises(InvalidToken):
        verify(tok, "unsubscribe", primary=SECRET_A, previous="")

def test_verify_with_rotated_secret():
    """A token signed with the old secret still verifies after rotation."""
    tok = sign(7, "manage", primary=SECRET_A, previous="")
    # Rotation: old primary becomes previous, new primary set
    assert verify(tok, "manage", primary=SECRET_B, previous=SECRET_A) == 7

def test_verify_fails_after_second_rotation():
    """A token signed with the original secret invalidates after two rotations."""
    tok = sign(7, "manage", primary=SECRET_A, previous="")
    # After two rotations: previous now holds the FIRST rotation's primary,
    # which is not SECRET_A.
    with pytest.raises(InvalidToken):
        verify(tok, "manage", primary="c" * 32, previous=SECRET_B)

def test_verify_fails_on_tampered_token():
    tok = sign(123, "confirm", primary=SECRET_A, previous="")
    tampered = tok[:-1] + ("A" if tok[-1] != "A" else "B")
    with pytest.raises(InvalidToken):
        verify(tampered, "confirm", primary=SECRET_A, previous="")
