#!/usr/bin/env python3
"""Apply published adapters to pinned core sources and verify installations."""
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(args, cwd, env, **kwargs):
    return subprocess.run(args, cwd=cwd, env=env, check=True, **kwargs)


def main():
    catalogs = sorted((ROOT / "patches/providers").glob("*/provider.patch"))
    assert catalogs, "No published provider patches"
    bases = {re.search(r"Core commit: `([0-9a-f]{40})`", (p.parent / "README.md").read_text()).group(1)
             for p in catalogs}
    assert len(bases) == 1, bases
    base = bases.pop()
    archive = subprocess.run(["git", "archive", base], cwd=ROOT, check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory(prefix="harn-patches-") as temporary:
        work = Path(temporary)
        selections = [()] + [(p,) for p in catalogs] + [tuple(catalogs)]
        for number, selection in enumerate(selections):
            source = work / ("source-" + str(number))
            source.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(source, filter="data")
            (source / ".core-revision").write_text(base + "\n")
            for patch in selection:
                run(["git", "apply", "--check", str(patch)], source, os.environ)
                run(["git", "apply", str(patch)], source, os.environ)
            # Verify that disjoint payloads also apply in reverse order.
            if len(selection) > 1:
                reverse = work / "reverse"
                reverse.mkdir()
                with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                    bundle.extractall(reverse, filter="data")
                for patch in reversed(selection):
                    run(["git", "apply", str(patch)], reverse, os.environ)
                for patch in selection:
                    name = patch.parent.name
                    for path in (source / "providers" / name).rglob("*"):
                        if path.is_file():
                            assert path.read_bytes() == (reverse / path.relative_to(source)).read_bytes()
            home = work / ("home-" + str(number))
            home.mkdir()
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / "config"),
                       XDG_STATE_HOME=str(home / "state"), XDG_CACHE_HOME=str(home / "cache"))
            names = [p.parent.name for p in selection]
            if names:
                includes, entries = [], []
                for name in names:
                    includes.append('#include "providers/' + name + '/provider.h"')
                    sample = (source / "providers" / name / "config.example.h").read_text()
                    block = sample.split('static const Connection connections[] = {', 1)[1].split('\n};', 1)[0]
                    entries.append(block.replace('    {0},', '').strip())
                (source / "config.h").write_text(
                    '#ifdef CONFIG_CONNECTIONS\n#undef CONFIG_CONNECTIONS\n#define PATCH_CONNECTIONS\n#endif\n'
                    '#include "config.def.h"\n#undef DEFAULT_PROVIDER\n#define DEFAULT_PROVIDER "' + names[0] + '"\n'
                    '#ifdef PATCH_CONNECTIONS\n' + '\n'.join(includes) + '\nstatic const Connection connections[] = {\n'
                    + '\n'.join(entries) + '\n    {0},\n};\n#endif\n')
            prefix = home / "installed"
            run(["make", "-j2"], source, env)
            run(["make", "install", "PREFIX=" + str(prefix)], source, env)
            installed = prefix / "bin/harn"
            output = run([str(installed), "--version"], source, dict(env, PATH="/nonexistent"), capture_output=True).stdout
            for name in names:
                assert (name + "@1").encode() in output, output
            if not names:
                assert len(output.splitlines()) == 1, output
                assert not (source / "providers").exists()
                assert not (source / "vendor/libjwt").exists()
                rejected = subprocess.run([str(installed)], cwd=source, env=env, input=b"", capture_output=True)
                assert rejected.returncode == 2 and b"No provider configured" in rejected.stderr
                assert not list((home / "state/harn/sessions").glob("*/journal.jsonl"))
                for option in ("--help", "--sessions"):
                    run([str(installed), option], source, dict(env, PATH="/nonexistent"), capture_output=True)
                run(["make", "check-core"], source, env)
            else:
                for name in names:
                    run(["python3", "providers/" + name + "/check.py"], source, env)
            print("Verified installation:", ", ".join(names) if names else "unpatched core", flush=True)
    print("All isolated and combined patch installations passed.")


if __name__ == "__main__":
    main()
