import os
import pytest

# Colour is a terminal affordance, and these tests assert content.
#
# Click's CliRunner hands rich a stream whose `isatty()` is True, so rich styles
# its output and assertions like `"Found 1 file" in result.output` fail against
# `"\x1b[1;33mFound 1 file\x1b[0m"`. Whether that bit depended on which test had
# already touched the environment, which is why this passed in a full run and
# failed in isolation.
#
# `NO_COLOR` is the cross-tool convention for exactly this and rich honours it,
# so the suite states its intent once here rather than having every test strip
# escape codes. Machine-readable output is a separate matter: `--json-output`
# does not go through rich at all, and a test enforces that.
os.environ.setdefault("NO_COLOR", "1")


# The language backend table now lives in composition rather than in the lowest
# layer, and is registered at each entrypoint. Most of this suite calls into
# `uta.shared` well below any entrypoint, so it registers here for the same
# reason the CLI does -- an unregistered backend raises at first use, which
# would be a fixture failure dressed up as a product one.
from uta.composition.language_backends import register_language_backends  # noqa: E402

register_language_backends()

from uta.app.persistence import register_task_persistence

register_task_persistence()



def pytest_ignore_collect(collection_path, config):
    path = str(collection_path)
    return f"{os.sep}tests{os.sep}fixtures{os.sep}python_projects{os.sep}" in path


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), "fixtures")
