# path: modules/text_styles.py

"""Deterministic unicode "fancy font" conversion for channel names,
nicknames, and embeds — see discord_bot/cogs/_views_style_wizard.py for
the wizard that surfaces this. Pure codepoint-offset character swaps, no
AI involved: results are instant and always exactly reproducible, unlike
asking an LLM to emit obscure unicode letter-by-letter (which is
unreliable and drifts between requests). AI is only used upstream of this
module, to suggest plain-text name *ideas* — the actual font conversion
always goes through here.

Most Unicode "Mathematical Alphanumeric Symbols" (U+1D400 block) and a
few other blocks used below are sequential A-Z/a-z/0-9 runs, so each
style is built from a single starting codepoint per case rather than a
hand-typed 26/26/10-character table — except for a handful of styles
where a few codepoints were already claimed by older unicode blocks
(letterlike symbols) before this block existed, which is what EXCEPTIONS
below patches per style.
"""

UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER = "abcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"


def _run(start: int, length: int) -> str:
    return "".join(chr(start + i) for i in range(length))


def _table(upper_start=None, lower_start=None, digit_start=None,
           upper_exceptions=None, lower_exceptions=None) -> dict:
    mapping = {}
    if upper_start is not None:
        chars = list(_run(upper_start, 26))
        for ch, repl in (upper_exceptions or {}).items():
            chars[UPPER.index(ch)] = repl
        mapping.update(dict(zip(UPPER, chars)))
    if lower_start is not None:
        chars = list(_run(lower_start, 26))
        for ch, repl in (lower_exceptions or {}).items():
            chars[LOWER.index(ch)] = repl
        mapping.update(dict(zip(LOWER, chars)))
    if digit_start is not None:
        mapping.update(dict(zip(DIGITS, _run(digit_start, 10))))
    return mapping


def _alpha(chars, both_cases=True) -> dict:
    """Map A-Z (and a-z too, when both_cases) onto a 26-item list of
    replacement characters — for styles that only have one alphabet."""
    chars = list(chars)
    assert len(chars) == 26, len(chars)
    mapping = dict(zip(UPPER, chars))
    if both_cases:
        mapping.update(dict(zip(LOWER, chars)))
    return mapping


def _combining(mark: str) -> dict:
    """Letters/digits followed by a combining mark (strikethrough, underline)."""
    return {ch: ch + mark for ch in UPPER + LOWER + DIGITS}


_SUPERSCRIPT = dict(zip(UPPER, "ᴬᴮᶜᴰᴱᶠᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾꟴᴿˢᵀᵁⱽᵂˣʸᶻ"))
_SUPERSCRIPT.update(zip(LOWER, "ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖᑫʳˢᵗᵘᵛʷˣʸᶻ"))
_SUPERSCRIPT.update(zip(DIGITS, "⁰¹²³⁴⁵⁶⁷⁸⁹"))


# key -> (display label, sample emoji, char map)
STYLES = {
    "bold": ("Bold", "𝐁", _table(0x1D400, 0x1D41A, 0x1D7CE)),
    # Italic lowercase h has no assigned codepoint in the math-alphanumeric
    # block (U+1D455 is reserved) — Unicode's own workaround reuses the
    # pre-existing Planck-constant italic h (U+210E) instead.
    "italic": ("Italic", "𝑰", _table(0x1D434, 0x1D44E, lower_exceptions={"h": "ℎ"})),
    "bold_italic": ("Bold Italic", "𝑩", _table(0x1D468, 0x1D482)),
    "sans": ("Sans", "𝖲", _table(0x1D5A0, 0x1D5BA, 0x1D7E2)),
    "sans_bold": ("Sans Bold", "𝗦", _table(0x1D5D4, 0x1D5EE, 0x1D7EC)),
    "sans_italic": ("Sans Italic", "𝘚", _table(0x1D608, 0x1D622)),
    "sans_bold_italic": ("Sans Bold Italic", "𝙎", _table(0x1D63C, 0x1D656)),
    "monospace": ("Monospace", "𝙼", _table(0x1D670, 0x1D68A, 0x1D7F6)),
    "double_struck": ("Double-struck", "𝕊", _table(
        0x1D538, 0x1D552, 0x1D7D8,
        upper_exceptions={"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"},
    )),
    "fraktur": ("Fraktur", "𝔉", _table(
        0x1D504, 0x1D51E,
        upper_exceptions={"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"},
    )),
    "bold_fraktur": ("Bold Fraktur", "𝕱", _table(0x1D56C, 0x1D586)),
    "script": ("Script", "𝒮", _table(
        0x1D49C, 0x1D4B6,
        upper_exceptions={"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"},
        lower_exceptions={"e": "ℯ", "g": "ℊ", "o": "ℴ"},
    )),
    "bold_script": ("Bold Script", "𝓢", _table(0x1D4D0, 0x1D4EA)),
    "fullwidth": ("Fullwidth", "Ｓ", _table(0xFF21, 0xFF41, 0xFF10)),
    "circled": ("Circled", "Ⓢ", _table(0x24B6, 0x24D0)),
    "small_caps": ("Small Caps", "ꜱ", _table(
        upper_start=None, lower_start=None,
    ) | {
        "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
        "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
        "o": "ᴏ", "p": "ᴘ", "q": "Q", "r": "ʀ", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ",
        "w": "ᴡ", "y": "ʏ", "z": "ᴢ",
    }),
    "squared": ("Squared", "🄰", _table(0x1F130, 0x1F130) ),
    "negative_circled": ("Black Circle", "🅐", _table(0x1F150, 0x1F150)),
    "negative_squared": ("Black Square", "🅰", _table(0x1F170, 0x1F170)),
    "superscript": ("Superscript", "ᵃ", _SUPERSCRIPT),
    "aesthetic": ("Aesthetic", "ᗩ", _alpha("ᗩᗷᑕᗪᗴᖴᏀᕼᏆᒎᛕᏞᗰᑎᗝᑭᑫᖇᔕᎢᑌᐯᗯ᙭Ꭹᘔ")),
    "currency": ("Currency", "₳", _alpha(["₳","฿","₵","Đ","Ɇ","₣","₲","Ⱨ","ł","J","₭","Ⱡ","₥","₦","Ø","₱","Q","Ɽ","₴","₮","Ʉ","V","₩","Ӿ","Ɏ","Ⱬ"])),
    "kanji": ("Kanji", "卂", _alpha(["卂","乃","匚","ㄉ","乇","千","Ꮆ","卄","丨","ﾌ","Ҝ","ㄥ","爪","几","ㄖ","卩","Ɋ","尺","丂","ㄒ","ㄩ","ᐯ","山","乂","ㄚ","乙"])),
    "strike": ("Strikethrough", "S̶", _combining("\u0336")),
    "underline": ("Underline", "U̲", _combining("\u0332")),
}
# Circled digits: 1-9 are the contiguous run U+2460-2468; 0 is separate (U+24EA).
STYLES["circled"][2].update({str(i): chr(0x2460 + i - 1) for i in range(1, 10)})
STYLES["circled"][2]["0"] = "⓪"

STYLE_ORDER = list(STYLES.keys())


def apply_style(text: str, key: str) -> str:
    """Convert `text` to the fancy-font `key`. Characters with no mapping
    for this style (spaces, punctuation, 'q'/'r'/'s'/'x' in small_caps
    where no distinct glyph exists, digits in styles with no digit
    variant, etc.) pass through unchanged rather than being dropped —
    channel names keep their hyphens/spaces this way."""
    mapping = STYLES[key][2]
    return "".join(mapping.get(ch, ch) for ch in text)


def style_choices() -> list:
    """[(key, label, emoji), ...] in STYLE_ORDER, for building a select menu."""
    return [(key, STYLES[key][0], STYLES[key][1]) for key in STYLE_ORDER]


_REVERSE = None


def to_plain(text: str) -> str:
    """Undo any fancy font so text can be restyled from scratch. Without this,
    a channel already named in (say) Fraktur passes through every other font
    unchanged, because the char maps only know plain A-Z/0-9."""
    import unicodedata
    global _REVERSE
    if _REVERSE is None:
        rev = {}
        for _label, _emoji, mapping in STYLES.values():
            for plain, fancy in mapping.items():
                base = fancy[0]
                if base != plain and ord(base) > 127:
                    rev.setdefault(base, plain.lower())
        _REVERSE = rev
    text = unicodedata.normalize("NFKC", text)
    text = "".join(_REVERSE.get(ch, ch) for ch in text)
    return "".join(ch for ch in text if ch not in ("\u0336", "\u0332"))


def channel_name(text: str, key: str, hyphenate: bool = True) -> str:
    """Text -> a Discord-channel-safe styled name. Discord channel names are
    lowercase, so the text is lowercased BEFORE the font is applied (a
    styled capital like U+1D40F would otherwise sneak a capital past
    Discord's own lowercasing, which only touches plain A-Z). Text/forum
    channels also can't hold spaces, so runs of whitespace become single
    hyphens (voice/stage channels keep spaces: hyphenate=False)."""
    cleaned = " ".join(to_plain(text).lower().split())
    if hyphenate:
        cleaned = cleaned.replace(" ", "-")
    return apply_style(cleaned, key)[:100]
