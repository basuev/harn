#!/usr/bin/env python3
"""Build, test, and install a release's sources without personal configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def run(args, cwd, env, **kwargs):
    return subprocess.run(args, cwd=cwd, env=env, check=True, **kwargs)


def main():
    archive = Path(sys.argv[1]).resolve()
    name = archive.name[:-len(".tar.gz")]
    with tempfile.TemporaryDirectory(prefix="harn-release-check-") as temporary:
        work = Path(temporary)
        with tarfile.open(archive) as bundle:
            bundle.extractall(work, filter="data")
        release = work / name
        source = release / "source"
        assert {p.name for p in release.iterdir()} == {
            "bin", "licenses", "source", "README.md", "DESIGN.md", "LICENSE"}
        forbidden = {".build", ".git", ".local", "dist", "__pycache__", "SYSTEM_ONE_RESEARCH.md"}
        for path in source.rglob("*"):
            assert not forbidden.intersection(path.relative_to(source).parts), path
        assert not (source / "harn").exists()
        for name in ("README.md", "DESIGN.md", "LICENSE"):
            assert (release / name).read_bytes() == (source / name).read_bytes()
        for name in (".gitignore", ".clang-format", "Makefile", "config.h", "harn.h"):
            assert (source / name).is_file(), name
        for library in ("libjwt", "linenoise"):
            assert (release / "licenses" / ("LICENSE." + library)).read_bytes() == (
                source / "vendor" / library / "LICENSE").read_bytes()

        home = work / "home"
        home.mkdir()
        scratch = work / "tmp"
        scratch.mkdir()
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "TMPDIR": str(scratch),
            "LC_ALL": "C.UTF-8",
            "TERM": "xterm",
        }
        run(["make", "-j2"], source, env)
        run(["make", "check"], source, env)
        prefix = home / ".local"
        run(["make", "install", "PREFIX=" + str(prefix)], source, env)
        installed = prefix / "bin/harn"
        assert installed.read_bytes() == (source / "harn").read_bytes()
        assert str(source).encode() not in installed.read_bytes()
        assert str(Path.home()).encode() not in (release / "bin/harn").read_bytes()
        assert (prefix / "share/licenses/harn/LICENSE").read_bytes() == (
            source / "LICENSE").read_bytes()
        for library in ("libjwt", "linenoise"):
            assert (prefix / "share/licenses/harn" / ("LICENSE." + library)).read_bytes() == (
                source / "vendor" / library / "LICENSE").read_bytes()

        # Native startup must not need a compiler or a language runtime on PATH.
        runtime = dict(env, PATH="/nonexistent")
        expected = run([str(source / "harn"), "--version"], source, runtime,
                       capture_output=True).stdout
        for binary in (installed, release / "bin/harn"):
            assert run([str(binary), "--version"], source, runtime,
                       capture_output=True).stdout == expected
        run([str(installed)], source, runtime, input=b"/context\n/quit\n", capture_output=True)
        journals = list((home / ".local/state/harn/sessions").glob("*/journal.jsonl"))
        assert len(journals) == 1
        records = [json.loads(line) for line in journals[0].read_text().splitlines()]
        assert not any(record["type"] == "request" for record in records)
        assert not (home / ".config/harn/credentials.json").exists()
    print("Release sources, fixture tests, user-prefix installation, licenses, and native startup verified.")


if __name__ == "__main__":
    main()
