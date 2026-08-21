import re

NAME_PATTERN = re.compile(r"[a-z][\-a-z0-9]*(\.[\-a-z0-9]+)*")
SHA256_STRING_PATTERN = r"^[a-fA-F0-9]{64}$"
