"""Stdlib-only launcher shared by UTA and the pinned dev-skills sparse checkout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import uuid
import xml.etree.ElementTree as ET


class PitCompatibilityError(OSError):
    """Terminal compatibility failure; an earlier green run cannot replace it.

    `modules` carries the completion states this invocation did record,
    `completed` the Maven run it belongs to, and `diagnostics` what the
    invocation's evidence directory actually held -- so a caller can report what
    the gates said, and a later reader can tell an incomplete run apart from one
    whose evidence was never written or was deleted underneath it.
    """

    modules = ()
    completed = None
    diagnostics: dict = {}


def reject_compatibility_failure(output: str) -> None:
    failures = [line for line in output.splitlines() if "[uta-pit-compat]" in line and "verified module=" not in line]
    if failures:
        raise PitCompatibilityError(failures[-1][:2000])


def _property(cmd: list[str], key: str) -> str | None:
    result = None
    for index, item in enumerate(cmd):
        value = cmd[index + 1] if item == "-D" and index + 1 < len(cmd) else item[2:] if item.startswith("-D") else ""
        if value.startswith(key + "="):
            result = value.split("=", 1)[1]
    return result


def prepare_command(cmd: list[str], repo: Path, *, artifact_dir: Path | None = None) -> list[str]:
    try:
        return _prepare_command(cmd, repo, artifact_dir=artifact_dir)
    except (OSError, ValueError, KeyError) as exc:
        raise PitCompatibilityError(str(exc)) from exc


def _prepare_command(cmd: list[str], repo: Path, *, artifact_dir: Path | None = None) -> list[str]:
    root = artifact_dir or Path(__file__).resolve().parent
    # Reports carry effective verify commands into repair. Metadata must not
    # inherit that invocation's extension or require its mutation completion.
    if "verify" not in cmd:
        result = []
        index = 0
        while index < len(cmd):
            item = cmd[index]
            separate = item == "-D" and index + 1 < len(cmd)
            value = cmd[index + 1] if separate else item[2:] if item.startswith("-D") else ""
            index += 2 if separate else 1
            if value.startswith("uta.pit.compat."):
                continue
            if value.startswith("maven.ext.class.path="):
                paths = [p for p in value.split("=", 1)[1].split(os.pathsep)
                         if p and Path(p).resolve() != (root / "pit-runtime-compat.jar").resolve()]
                if paths:
                    result.append("-Dmaven.ext.class.path=" + os.pathsep.join(paths))
                continue
            result.extend([item, value] if separate else [item])
        return result
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        jar = root / "pit-runtime-compat.jar"
        if hashlib.sha256(jar.read_bytes()).hexdigest() != manifest["sha256"]:
            raise OSError("PIT compatibility artifact digest mismatch")
    except (ValueError, KeyError) as exc:
        raise OSError("Invalid PIT compatibility artifact manifest") from exc
    # Do not silently replace extensions supplied outside the argv we control.
    for name in ("MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
        if "maven.ext.class.path" in os.environ.get(name, ""):
            raise OSError("Move existing JVM extension configuration to Maven -Dmaven.ext.class.path before enforcement")
    for name in ("maven.config", "jvm.config"):
        config = Path(repo) / ".mvn" / name
        if config.exists() and "maven.ext.class.path" in config.read_text():
            raise OSError("Move existing .mvn extension classpath to the enforcement command")
    for key in ("skipPitest", "targetClasses", "excludedMethods"):
        if _property(cmd, key) is not None:
            raise OSError("Reserved diff-enforcement scope property: " + key)
    existing = _property(cmd, "maven.ext.class.path") or ""
    paths = [p for p in existing.split(os.pathsep) if p and p != str(jar)]
    paths.append(str(jar))
    result = []
    skip = False
    for index, item in enumerate(cmd):
        if skip:
            skip = False
            continue
        if item == "-D" and index + 1 < len(cmd) and cmd[index + 1].startswith("maven.ext.class.path="):
            skip = True
            continue
        if not item.startswith("-Dmaven.ext.class.path=") and not item.startswith("-Duta.pit.compat."):
            result.append(item)
    nonce = uuid.uuid4().hex
    directory = Path(repo).resolve() / ".uta_cache" / "pit-compat" / nonce
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    result += ["-Dmaven.ext.class.path=" + os.pathsep.join(paths),
               "-Duta.pit.compat.intent=diff-enforcement",
               "-Duta.pit.compat.invocation=" + nonce,
               "-Duta.pit.compat.evidence=" + str(directory / "completion.xml"),
               "-Dtest.enforcement.enabled=true"]
    if _property(result, "targetTests") is None:
        # Full CI means the actual suite, not a historical POM targetTests pin.
        # Targeted repair and baseline reruns keep their explicit selection.
        result.append("-DtargetTests=*")
    if _property(result, "test.enforcement.pitest.testStrengthThreshold") is None:
        result.append("-Dtest.enforcement.pitest.testStrengthThreshold=100")
    if _property(result, "skipFailingTests") is None:
        # A diff-unrelated red test aborts PIT before any mutant runs, so a module
        # with legacy rot yields no verdict at all. PIT's own answer is to drop
        # those results from the coverage pass; the extension binds the property
        # the plugin descriptor leaves unreachable. The vacuous score this would
        # otherwise permit -- every mutant uncovered, test strength 100% -- is
        # refused by the classifier, not by keeping the abort.
        result.append("-DskipFailingTests=true")
    return result


def validate_completion(cmd: list[str]) -> dict:
    filename = _property(cmd, "uta.pit.compat.evidence")
    if not filename:
        return {}
    path = Path(filename)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise OSError("PIT compatibility completion evidence exceeds size bound")
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        # Post-hoc forensics keeps losing this race: the evidence tree is inside
        # the workspace and does not outlive it. Say now which of the three
        # cases this was, because nothing downstream can tell them apart later.
        raise _unreadable(path, exc) from exc
    if root.tag != "pitCompatibility" or root.get("invocation") != _property(cmd, "uta.pit.compat.invocation"):
        mismatch = PitCompatibilityError("PIT compatibility completion invocation mismatch")
        mismatch.diagnostics = {
            "evidencePath": str(path),
            "evidenceState": "invocation-mismatch",
            "recorded": root.get("invocation"),
            "expected": _property(cmd, "uta.pit.compat.invocation"),
        }
        raise mismatch
    modules = [dict(item.attrib) for item in root.findall("module")]
    if root.get("complete") != "true":
        raise _incomplete("PIT compatibility did not verify every obligated module", modules, path)
    if any(m.get("state") not in {"skipped", "completed"} for m in modules):
        raise _incomplete("PIT compatibility module completion is missing", modules, path)
    return {"invocation": root.get("invocation"), "modules": modules, "artifactVersion": "1.0.0"}


def _incomplete(message: str, modules: list, path: "Path") -> PitCompatibilityError:
    unverified = [m.get("id") or "?" for m in modules if m.get("state") not in {"skipped", "completed"}]
    if unverified:
        message += ": " + ", ".join(unverified)
    error = PitCompatibilityError(message)
    error.modules = modules
    error.diagnostics = {"evidencePath": str(path), "evidenceState": "parsed", "modules": modules}
    return error


def _unreadable(path: "Path", cause: Exception) -> PitCompatibilityError:
    """Distinguish evidence never written, evidence deleted, and evidence corrupt."""
    directory = path.parent
    if not directory.exists():
        state, detail = "directory-missing", "invocation evidence directory is gone: " + str(directory)
    elif not path.exists():
        state, detail = "file-missing", "Maven wrote no completion evidence into " + str(directory)
    else:
        state, detail = "unparsable", "%s: %s" % (type(cause).__name__, cause)
    diagnostics = {"evidencePath": str(path), "evidenceState": state, "detail": detail}
    if directory.exists():
        try:
            diagnostics["directoryEntries"] = sorted(p.name for p in directory.iterdir())[:50]
        except OSError:
            pass
    error = PitCompatibilityError(
        "Missing or invalid PIT compatibility completion evidence (%s) -- %s" % (state, detail)
    )
    error.diagnostics = diagnostics
    return error


def _stop_group(process: subprocess.Popen) -> None:
    # Maven forks Surefire/PIT children: parent exit does not imply tree exit.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def main(argv: list[str]) -> int:
    command = argv[1:] if argv[:1] == ["--"] else argv
    if not command or "verify" not in command:
        print("Java enforcement requires: launcher.py -- mvn ... verify", file=sys.stderr)
        return 2
    try:
        command = prepare_command(command, Path.cwd())
        process = subprocess.Popen(command, start_new_session=True)
        def terminate(*_):
            _stop_group(process)
            raise SystemExit(143)
        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            _stop_group(process)
            return 130
        finally:
            signal.signal(signal.SIGTERM, previous)
        if code == 0:
            validate_completion(command)
        return code if code >= 0 else 128 - code
    except OSError as exc:
        print("PIT compatibility failed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
