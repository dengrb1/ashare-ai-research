"""Command implementations for the ``ashare-ai`` CLI.

These live in a package rather than beside :mod:`ashare_ai.cli` so they can be
imported lazily by the argparse entry point without pulling in the API stack.
"""
