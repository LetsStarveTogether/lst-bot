from textwrap import wrap
from typing import Annotated, override

from pydantic import BaseModel, Field

QUOTE_WIDTH = 12
QUOTE_SPACE = "\u3000"
QUOTE_CORNERS = "┌┐└┘"
SOURCE_QUOTES = "《》"


class Hitokoto(BaseModel):
    hitokoto: str
    from_: Annotated[str, Field(alias="from")]
    from_who: str | None

    @override
    def __str__(self) -> str:
        quote_lines = [
            wrapped
            for line in self.hitokoto.strip().splitlines()
            for wrapped in (wrap(line, width=QUOTE_WIDTH) or [""])
        ] or [""]
        quote = "\n".join([
            f"{QUOTE_CORNERS[0]}{QUOTE_SPACE * QUOTE_WIDTH}{QUOTE_CORNERS[1]}",
            *(f"{QUOTE_SPACE}{line}" for line in quote_lines),
            f"{QUOTE_CORNERS[2]}{QUOTE_SPACE * QUOTE_WIDTH}{QUOTE_CORNERS[3]}",
        ])
        author = self.from_who.strip() if self.from_who else ""
        source = self.from_.strip()
        if source:
            if SOURCE_QUOTES[0] not in source:
                source = f"{SOURCE_QUOTES[0]}{source}"
            if SOURCE_QUOTES[1] not in source:
                source = f"{source}{SOURCE_QUOTES[1]}"
        signature = f"{author}{source}"
        if not signature:
            return quote
        signature_line = f"—— {signature}"
        signature_indent = QUOTE_SPACE * max(QUOTE_WIDTH + 4 - len(signature_line), 0)
        return f"{quote}\n{signature_indent}{signature_line}"
