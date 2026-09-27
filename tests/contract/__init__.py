"""Package marker for the repository contract suite.

This marker is load-bearing, not cosmetic. ``tests/`` is not a package, so
pytest's default ``prepend`` import mode names a test module after its file
stem; ``tests/contract/test_config_documents.py`` would then be imported as
``test_config_documents``, exactly like the unrelated
``tests/config/test_config_documents.py`` of the configuration work package, and
the whole session aborts with ``import file mismatch`` before a single test runs
(that includes the full ``python -m pytest`` coverage gate of
``docs/testing-policy.md``).

Making this directory a package gives its modules the qualified name
``contract.test_config_documents`` while leaving ``tests/config`` untouched, so
both suites collect and the five contract modules keep the file names the
specification imposes on them.
"""
