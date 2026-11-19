from pathlib import Path

import verifier


def test_verifier_resolves_to_its_own_source_tree() -> None:
    # The repo root also has a folder called verifier/. The import must resolve to the
    # real package in verifier/src, not to that folder as an empty namespace package.
    assert verifier.__file__ is not None
    assert Path(verifier.__file__).parent.parent.name == "src"
