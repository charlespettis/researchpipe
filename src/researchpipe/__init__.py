"""researchpipe: research-driven open-source security tooling opportunity pipeline."""


def main() -> int:
    from .cli import main as _main

    return _main()
