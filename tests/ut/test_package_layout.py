from pathlib import Path

import pytest

pytestmark = pytest.mark.ut

SRC = Path(__file__).resolve().parents[2] / "src"


def test_package_is_imported_from_src_without_build() -> None:
    """The project is not built, so every installed artifact is hash-locked in uv.lock."""
    import digital_souls_memory_hindsight

    assert Path(digital_souls_memory_hindsight.__file__).resolve().is_relative_to(SRC)


def test_project_itself_is_not_installed() -> None:
    from importlib.metadata import PackageNotFoundError, version

    with pytest.raises(PackageNotFoundError):
        version("digital-souls-memory-hindsight")
