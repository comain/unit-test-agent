"""Metadata response for execution-focused Maven fakes, not version-check tests."""

from functools import wraps
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

DIFF_MUTATION_OK = (
    "[test-enforcer] diff mutation score 100.00% passed for fixture "
    "(1/1 detected; 0 survived; 0 no coverage excluded)\n"
)


def with_completion(cmd, result):
    """Execution fakes explicitly model the separate completion contract."""
    evidence = next((str(item).split("=", 1)[1] for item in cmd if str(item).startswith("-Duta.pit.compat.evidence=")), None)
    if evidence:
        nonce = next(str(item).split("=", 1)[1] for item in cmd if str(item).startswith("-Duta.pit.compat.invocation="))
        ET.ElementTree(ET.Element("pitCompatibility", invocation=nonce, complete="true")).write(evidence)
    return result


def with_resolved_enforcer(run):
    """Model a supported reactor before exercising the caller's verify behavior."""
    @wraps(run)
    def wrapped(cmd, *args, **kwargs):
        if "help:effective-pom" in cmd:
            output = next(str(item).split("=", 1)[1] for item in cmd if str(item).startswith("-Doutput="))
            Path(output).write_text("""<project><build><plugins><plugin>
            <groupId>com.example.build.maven-plugins</groupId><artifactId>test-enforcer</artifactId>
            <version>1.0.16</version></plugin></plugins></build></project>""")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        result = run(cmd, *args, **kwargs)
        return with_completion(cmd, result) if result.returncode == 0 else result
    return wrapped
