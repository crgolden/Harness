"""The one grammar every hook token shares: the token in capitals, opening its own line (optionally wrapped in
markdown bold), then a colon and a payload on that same line. Every hook builds its token patterns here, so no
token can be read one way by one hook and another way by the next."""

import re


def token_line(token):
    """The line pattern for a token, its payload captured."""
    return re.compile(r"^[ \t]*(?:\*\*)?" + token + r":(?:\*\*)?[ \t]*(\S[^\r\n]*?)[ \t]*\r?$", re.MULTILINE)
