import pytest
import os


def pytest_ignore_collect(collection_path, config):
    path = str(collection_path)
    return f"{os.sep}tests{os.sep}fixtures{os.sep}python_projects{os.sep}" in path


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), "fixtures")
