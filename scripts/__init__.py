"""Repository-owned command package.

Making ``scripts`` a regular package keeps an ambient package of the same name from winning
over this checkout's command modules before their own entrypoint guards can run.
"""
