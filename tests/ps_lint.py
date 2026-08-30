"""A static parse of the PowerShell constructs that broke on Windows PowerShell 5.1.

There is no PowerShell on the build host, so this stands in for one. It walks a
script the way the parser does — block comments, line comments, here-strings,
single/double-quoted strings with their escape rules, and bracket nesting — and
reports unterminated strings and unbalanced brackets.

Two details make it faithful to the bug it exists to prevent:

* Windows PowerShell 5.1 reads a BOM-less script as ANSI, NOT UTF-8.
* PowerShell accepts SMART quotes as real string delimiters.

Together those turn a harmless-looking em dash (UTF-8 `E2 80 94`) or box-drawing
rule (`E2 94 80`) into a string delimiter, because byte 0x94 decodes to U+201D
in ANSI — even inside a comment. That is what produced "unexpected token '}'"
at lines 115/157 and "string missing terminator" further down, on a file that
was perfectly valid UTF-8. `check()` therefore enforces ASCII-only + BOM, and
`scan()` models the smart-quote behaviour so a regression is caught here rather
than on the owner's PC.
"""
SMART = "‘’“”–—"

def scan(text):
    """Walk the script the way the parser does: strings, comments, nesting.
    Returns (errors, depth_map)."""
    errs, i, n = [], 0, len(text)
    line = 1
    stack = []
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1; i += 1; continue
        # block comment
        if text.startswith("<#", i):
            end = text.find("#>", i + 2)
            if end == -1:
                errs.append(f"line {line}: unterminated block comment <#"); break
            line += text.count("\n", i, end); i = end + 2; continue
        # line comment
        if c == "#":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        # here-strings
        if text.startswith('@"', i) or text.startswith("@'", i):
            q = text[i+1]; term = f'\n{q}@'
            end = text.find(term, i)
            if end == -1:
                errs.append(f"line {line}: unterminated here-string @{q}"); break
            line += text.count("\n", i, end); i = end + len(term); continue
        # single-quoted. PowerShell also accepts the smart variants as real
        # delimiters, which is precisely why a mis-decoded em dash breaks it.
        if c in "'\u2018\u2019":
            j = i + 1
            while j < n:
                if text[j] in "'\u2018\u2019":
                    if j + 1 < n and text[j+1] == text[j]:
                        j += 2; continue
                    break
                if text[j] == "\n": line += 1
                j += 1
            if j >= n:
                errs.append(f"line {line}: unterminated single-quoted string"); break
            i = j + 1; continue
        # double-quoted (smart variants included, as PowerShell does)
        if c in '"\u201c\u201d':
            j = i + 1
            while j < n:
                if text[j] == "`":
                    j += 2; continue
                if text[j] in '"\u201c\u201d':
                    if j + 1 < n and text[j+1] == text[j]:
                        j += 2; continue
                    break
                if text[j] == "\n": line += 1
                j += 1
            if j >= n:
                errs.append(f"line {line}: unterminated double-quoted string"); break
            i = j + 1; continue
        if c == "`":            # escape / line continuation
            i += 2; continue
        if c in "({[":
            stack.append((c, line)); i += 1; continue
        if c in ")}]":
            pair = {")": "(", "}": "{", "]": "["}[c]
            if not stack:
                errs.append(f"line {line}: unexpected token '{c}'")
            elif stack[-1][0] != pair:
                errs.append(f"line {line}: '{c}' closes '{stack[-1][0]}' opened at line {stack[-1][1]}")
                stack.pop()
            else:
                stack.pop()
            i += 1; continue
        i += 1
    for ch, ln in stack:
        errs.append(f"line {ln}: unclosed '{ch}'")
    return errs


def check(path):
    raw = open(path, "rb").read()
    errs = []
    bom = raw[:3] == b"\xef\xbb\xbf"
    body = raw[3:] if bom else raw
    if not bom:
        errs.append("missing UTF-8 BOM (Windows PowerShell 5.1 would read it as ANSI)")
    non_ascii = sorted({b for b in body if b > 127})
    if non_ascii:
        errs.append(f"non-ASCII bytes present: {[hex(b) for b in non_ascii][:8]}")
    text = body.decode("utf-8", "replace")
    for ch in SMART:
        if ch in text:
            errs.append(f"smart punctuation {ch!r} present (PowerShell treats it as a quote)")
    errs += scan(text)
    return errs


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        e = check(p)
        print(f"{p}: {'OK - parses clean' if not e else 'FAILED'}")
        for x in e:
            print("   ", x)
        if e:
            sys.exit(1)
