from pathlib import Path

# This tool merges example files into the docs and thus ensures
# that the tested version of the example is also the one in the
# doc.


class TOP:
    """Top level of the Markdown doc"""

    def next_state(line, location):
        if line.startswith(TRIPLE_QUOTE):
            return START_EXAMPLE, [line]
        else:
            return TOP, [line]


class OPEN:
    """Example is open and being copied"""

    def next_state(line, location):
        if line.startswith(TRIPLE_QUOTE):
            check_triple_quote_alone(line, location)
            return TOP, [line]
        else:
            return OPEN, [line]


class REPLACING:
    """Code example is open being replaced because it corresponds to a file on disk"""

    def next_state(line, location):
        if line.startswith(TRIPLE_QUOTE):
            check_triple_quote_alone(line, location)
            return TOP, [line]
        else:
            return REPLACING, []  # IGNORE lines to be replaced


class START_EXAMPLE:
    """First line of a code example"""

    def next_state(line, location):
        if line.startswith("#"):
            filename = line.split("#")[1].strip()
            assert Path(filename).exists(), location
            with Path(filename).open() as f:
                example_lines = f.readlines()
            print("Replacing", filename)
            return REPLACING, [line] + example_lines
        else:
            return OPEN, [line]


def check_triple_quote_alone(line, location):
    """Triple-quotes at the ends or middle of line are
    almost certainly errors."""
    if line.strip() != TRIPLE_QUOTE:
        assert 0, location


TRIPLE_QUOTE = "```"


def replace_samples(input_file) -> str:
    """Read an input file line by line and generate a string
    to write out."""
    lines = list(input_file)
    output = []
    state = TOP
    for linenum, line in enumerate(lines):
        location = f"{input_file.name}:{linenum} : {line}"
        assert state in (TOP, OPEN, REPLACING, START_EXAMPLE)
        state, out = state.next_state(line, location)
        output.extend(out)
        if not line.startswith(TRIPLE_QUOTE):
            assert TRIPLE_QUOTE not in line, location
    return "".join(output)


def main(filename):
    with open(filename) as f:
        output = replace_samples(f)
    with open(filename, "w") as f:
        f.write(output)


filename = "docs/index.md"
main(filename)