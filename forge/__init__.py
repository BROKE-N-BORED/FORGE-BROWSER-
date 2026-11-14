# Forge Browser's parent package version. It mirrors
# forge/browser/__init__.py — one version for the repo, pinned by
# the suite (see VersionTests). The file stays because the tree
# ships a real forge/ package (explicit layout, predictable
# shadowing if this tree and Code Forge's forge/ ever share a
# sys.path); it used to say "2.5", the Code Forge parent version,
# which gave a reader two answers for one question.
__version__ = "0.9"
