import secrets

from django.utils.crypto import salted_hmac


def generate_otp(length=6):
    return f"{secrets.randbelow(10**length):0{length}d}"


def hash_otp(email, otp):
    return salted_hmac(
        "authentication.otp", f"{email}:{otp}", algorithm="sha256"
    ).hexdigest()
