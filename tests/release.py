#!/usr/bin/env python3
"""Verify an unpatched core archive, fixture checks and native installation."""
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
        assert {p.name for p in release.iterdir()} == {"bin", "licenses", "source"}
        forbidden = {".build", ".git", ".local", "dist", "__pycache__"}
        assert not (source / "providers").exists()
        for path in source.rglob("*"):
            assert not forbidden.intersection(path.relative_to(source).parts), path
        assert not (source / "harn").exists()
        assert not (source / "config.h").exists()
        assert not (source / "vendor/libjwt").exists()
        binary = (release / "bin/harn").read_bytes()
        for secret in (b"api.allmodels.io", b"api.refuseless.com", b"chatgpt.tokens.use.direct"):
            assert secret not in binary
        assert (source / "docs/provider-patches.md").is_file()
        assert (release / "licenses/LICENSE").read_bytes() == (source / "LICENSE").read_bytes()
        assert (release / "licenses/LICENSE.linenoise").read_bytes() == (source / "vendor/linenoise/LICENSE").read_bytes()

        home = work / "home"
        home.mkdir()
        scratch = work / "tmp"
        scratch.mkdir()
        env = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local/state"),
            "XDG_CACHE_HOME": str(home / ".cache"), "TMPDIR": str(scratch),
            "LC_ALL": "C.UTF-8", "TERM": "xterm",
        }
        run(["make", "-j2"], source, env)
        run(["make", "check"], source, env)
        prefix = home / ".local"
        run(["make", "install", "PREFIX=" + str(prefix)], source, env)
        installed = prefix / "bin/harn"
        assert installed.read_bytes() == (source / "harn").read_bytes()
        assert str(source).encode() not in installed.read_bytes()
        assert str(Path.home()).encode() not in binary
        assert (prefix / "share/licenses/harn/LICENSE").read_bytes() == (source / "LICENSE").read_bytes()
        assert not (prefix / "share/licenses/harn/LICENSE.libjwt").exists()
        runtime = dict(env, PATH="/nonexistent")
        for executable in (installed, release / "bin/harn"):
            for option in ("--help", "--version", "--sessions"):
                run([str(executable), option], source, runtime, capture_output=True)
            result = subprocess.run([str(executable)], cwd=source, env=runtime,
                                    input=b"", capture_output=True, timeout=5)
            assert result.returncode == 2, result
            assert b"No provider configured" in result.stderr
        assert not list((home / ".local/state/harn/sessions").glob("*/journal.jsonl"))
        assert not (home / ".config/harn/credentials.json").exists()
    print("Unpatched release, fixture checks, licenses and native installation verified.")


if __name__ == "__main__":
    main()
